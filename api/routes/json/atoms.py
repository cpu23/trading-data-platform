"""Bounded, allowlisted analysis-atom queries for JSON and HTMX consumers."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from config import load_config
from db import query_many
from serializers import isoformat

router = APIRouter()

SUBJECT_TYPES = {
    "macro_series",
    "regime",
    "econ_event",
    "story_cluster",
    "market",
    "portfolio",
}
_LIST_LIMIT = 100
_HISTORY_LIMIT = 50
_EVIDENCE_LIMIT = 20

_ATOM_SELECT = """
    SELECT a.id, a.subject_type, a.subject_id, a.claim_type, a.claim,
           a.observation_text, a.interpretation_text, a.scenario_text,
           a.unknowns, a.affected_assets, a.time_horizon, a.confidence,
           a.valid_from, a.expires_at, a.status, a.model_slug,
           a.prompt_version, a.published_at, a.created_at, e.evidence
    FROM analysis_atoms a
    LEFT JOIN LATERAL (
        SELECT json_agg(json_build_object(
                   'evidence_type', v.evidence_type,
                   'evidence_id', v.evidence_id,
                   'relationship', v.relationship)) AS evidence
        FROM (
            SELECT evidence_type, evidence_id, relationship
            FROM analysis_atom_evidence
            WHERE atom_id = a.id
            ORDER BY evidence_type, evidence_id
            LIMIT :evidence_limit
        ) v
    ) e ON TRUE
"""


def _validate_subject(subject_type: str | None, subject_id: str | None) -> None:
    if subject_type is not None and subject_type not in SUBJECT_TYPES:
        raise ValueError("unsupported subject_type")
    if subject_id is not None and (
        not subject_id.strip() or len(subject_id.strip()) > 120
    ):
        raise ValueError("invalid subject_id")


def _serialize_atom(row: dict) -> dict:
    evidence = row.get("evidence") or []
    return {
        "id": str(row["id"]),
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "claim_type": row["claim_type"],
        "claim": row["claim"],
        "observation_text": row.get("observation_text"),
        "interpretation_text": row.get("interpretation_text"),
        "scenario_text": row.get("scenario_text"),
        "unknowns": row.get("unknowns") or [],
        "affected_assets": row.get("affected_assets") or [],
        "time_horizon": row["time_horizon"],
        "confidence": row.get("confidence"),
        "valid_from": isoformat(row.get("valid_from")),
        "expires_at": isoformat(row.get("expires_at")),
        "status": row["status"],
        "model_slug": row.get("model_slug"),
        "prompt_version": row.get("prompt_version"),
        "published_at": isoformat(row.get("published_at")),
        "evidence": [
            {
                "evidence_type": item.get("evidence_type"),
                "evidence_id": str(item.get("evidence_id")),
                "relationship": item.get("relationship"),
            }
            for item in evidence
            if isinstance(item, dict)
        ],
    }


def load_atom_context(
    config: dict,
    *,
    subject_type: str | None = None,
    subject_id: str | None = None,
    limit: int = 50,
    include_history: bool = False,
) -> dict:
    """Current atoms plus optional bounded audit history; validation first."""
    _validate_subject(subject_type, subject_id)
    bounded_limit = max(1, min(_LIST_LIMIT, int(limit)))
    rows = query_many(
        _ATOM_SELECT
        + """
    WHERE a.status IN ('validated', 'published')
      AND (:subject_type IS NULL OR a.subject_type = :subject_type)
      AND (:subject_id IS NULL OR a.subject_id = :subject_id)
    ORDER BY a.valid_from DESC, a.id DESC
    LIMIT :limit""",
        params={
            "subject_type": subject_type,
            "subject_id": subject_id.strip() if subject_id else None,
            "limit": bounded_limit,
            "evidence_limit": _EVIDENCE_LIMIT,
        },
        config=config,
    )
    atoms = [_serialize_atom(row) for row in rows]
    if include_history and atoms:
        history_rows = query_many(
            """SELECT subject_type, subject_id, status, confidence, valid_from,
                      expires_at, updated_at, supersedes_atom_id
               FROM analysis_atoms
               WHERE status NOT IN ('validated', 'published')
               ORDER BY updated_at DESC
               LIMIT :limit""",
            params={"limit": _HISTORY_LIMIT * 2},
            config=config,
        )
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in history_rows:
            grouped.setdefault((row["subject_type"], row["subject_id"]), []).append(
                {
                    "status": row["status"],
                    "confidence": row.get("confidence"),
                    "valid_from": isoformat(row.get("valid_from")),
                    "expires_at": isoformat(row.get("expires_at")),
                    "updated_at": isoformat(row.get("updated_at")),
                    "supersedes_atom_id": str(row["supersedes_atom_id"])
                    if row.get("supersedes_atom_id")
                    else None,
                }
            )
        for atom in atoms:
            atom["history"] = grouped.get(
                (atom["subject_type"], atom["subject_id"]), []
            )
    return {"status": "published", "atoms": atoms}


def load_atom_detail(config: dict, atom_id: str) -> dict | None:
    try:
        parsed = UUID(atom_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid atom id") from None
    rows = query_many(
        _ATOM_SELECT + " WHERE a.id = :id LIMIT 1",
        params={"id": parsed, "evidence_limit": _EVIDENCE_LIMIT},
        config=config,
    )
    if not rows:
        return None
    atom = _serialize_atom(rows[0])
    history_rows = query_many(
        """SELECT id, status, confidence, valid_from, expires_at, updated_at,
                  supersedes_atom_id
           FROM analysis_atoms
           WHERE subject_type = :subject_type AND subject_id = :subject_id
           ORDER BY valid_from DESC, created_at DESC
           LIMIT :limit""",
        params={
            "subject_type": atom["subject_type"],
            "subject_id": atom["subject_id"],
            "limit": _HISTORY_LIMIT,
        },
        config=config,
    )
    atom["history"] = [
        {
            "id": str(row["id"]),
            "status": row["status"],
            "confidence": row.get("confidence"),
            "valid_from": isoformat(row.get("valid_from")),
            "expires_at": isoformat(row.get("expires_at")),
            "updated_at": isoformat(row.get("updated_at")),
            "supersedes_atom_id": str(row["supersedes_atom_id"])
            if row.get("supersedes_atom_id")
            else None,
        }
        for row in history_rows
    ]
    return atom


@router.get("/analysis/atoms")
def get_analysis_atoms(
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
):
    config = load_config()
    try:
        context = load_atom_context(
            config,
            subject_type=subject_type,
            subject_id=subject_id,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"atoms": context["atoms"], "limit": limit}


@router.get("/analysis/atoms/{atom_id}")
def get_analysis_atom(atom_id: str):
    config = load_config()
    try:
        atom = load_atom_detail(config, atom_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    if atom is None:
        raise HTTPException(status_code=404, detail="Unknown atom")
    return {"atom": atom}
