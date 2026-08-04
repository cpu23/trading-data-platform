from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from config import load_config
from db import query_many, query_one
from staleness import get_staleness_config, is_stale

router = APIRouter()


@router.get("/regime/current")
def get_regime_current():
    config = load_config()
    thresholds = get_staleness_config(config)

    sql = """
        SELECT rc.classification_id, rc.created_at, rc.scope, rc.regime,
               rc.sub_regime, rc.confidence, rc.supporting_data,
               so.opinion_id, so.direction, so.summary, so.key_factors,
               so.reasoning
        FROM regime_classifications rc
        JOIN structured_opinions so ON rc.opinion_id = so.opinion_id
        WHERE so.lifecycle_status = 'published'
        ORDER BY rc.created_at DESC
        LIMIT 1
    """
    row = query_one(sql, config=config)
    if row is None:
        return {
            "classification_id": None,
            "created_at": None,
            "stale": True,
            "stale_reason": "No regime classification available",
            "scope": None,
            "regime": None,
            "sub_regime": None,
            "direction": None,
            "confidence": None,
            "summary": None,
            "key_factors": [],
            "momentum_implications": None,
            "caution_flags": [],
            "opinion_id": None,
        }

    import json

    supporting = row.get("supporting_data", {})
    if isinstance(supporting, str):
        try:
            supporting = json.loads(supporting)
        except (json.JSONDecodeError, TypeError):
            supporting = {}

    key_factors = row.get("key_factors", [])
    if isinstance(key_factors, str):
        try:
            key_factors = json.loads(key_factors)
        except (json.JSONDecodeError, TypeError):
            key_factors = []

    stale, stale_reason = is_stale(row["created_at"], thresholds["regime_hours"])

    return {
        "classification_id": str(row["classification_id"]),
        "created_at": row["created_at"].isoformat()
        if hasattr(row["created_at"], "isoformat")
        else str(row["created_at"]),
        "stale": stale,
        "stale_reason": stale_reason,
        "scope": row["scope"],
        "regime": row["regime"],
        "sub_regime": row["sub_regime"],
        "direction": row.get("direction"),
        "confidence": row.get("confidence"),
        "summary": row.get("summary"),
        "key_factors": key_factors,
        "momentum_implications": supporting.get("momentum_implications"),
        "caution_flags": supporting.get("caution_flags", []),
        "opinion_id": str(row["opinion_id"]) if row.get("opinion_id") else None,
    }


@router.get("/regime/history")
def get_regime_history(days: int = Query(default=30, ge=1, le=365)):
    config = load_config()
    cutoff = datetime.now(UTC) - timedelta(days=days)

    sql = """
        SELECT rc.classification_id, rc.created_at, rc.scope, rc.regime,
               rc.sub_regime, rc.confidence,
               so.direction, so.summary, so.opinion_id
        FROM regime_classifications rc
        JOIN structured_opinions so ON rc.opinion_id = so.opinion_id
        WHERE rc.created_at >= :cutoff
          AND so.lifecycle_status = 'published'
        ORDER BY rc.created_at DESC
    """
    rows = query_many(sql, params={"cutoff": cutoff}, config=config)

    results = []
    for row in rows:
        results.append(
            {
                "classification_id": str(row["classification_id"]),
                "created_at": row["created_at"].isoformat()
                if hasattr(row["created_at"], "isoformat")
                else str(row["created_at"]),
                "scope": row["scope"],
                "regime": row["regime"],
                "sub_regime": row["sub_regime"],
                "confidence": row.get("confidence"),
                "direction": row.get("direction"),
                "summary": row.get("summary"),
                "opinion_id": str(row["opinion_id"]) if row.get("opinion_id") else None,
            }
        )

    return {"regimes": results, "days": days}
