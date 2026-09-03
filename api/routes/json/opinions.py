from fastapi import APIRouter, Query

from config import load_config
from db import query_many
from serializers import isoformat

router = APIRouter()


def _serialize_opinion(row: dict) -> dict:
    return {
        "opinion_id": str(row["opinion_id"]),
        "created_at": isoformat(row["created_at"]),
        "opinion_type": row["opinion_type"],
        "scope": row["scope"],
        "direction": row.get("direction"),
        "confidence": row.get("confidence"),
        "timeframe": row.get("timeframe"),
        "summary": row.get("summary"),
        "model_used": row.get("model_used"),
        "prompt_version": row.get("prompt_version"),
    }


def _query_opinions(where_clause: str = "", params: dict | None = None) -> list[dict]:
    config = load_config()
    sql = f"""
        SELECT opinion_id, created_at, opinion_type, scope, direction,
               confidence, timeframe, summary, model_used, prompt_version
        FROM structured_opinions
        WHERE lifecycle_status = 'published' {where_clause}
        ORDER BY created_at DESC
        LIMIT :limit
    """
    rows = query_many(sql, params=params, config=config)
    return [_serialize_opinion(row) for row in rows]


@router.get("/opinions/latest")
def get_opinions_latest(limit: int = Query(default=20, ge=1, le=200)):
    return {
        "opinions": _query_opinions(params={"limit": limit}),
        "limit": limit,
    }


@router.get("/opinions/{opinion_type}")
def get_opinions_by_type(
    opinion_type: str, limit: int = Query(default=20, ge=1, le=200)
):
    return {
        "opinions": _query_opinions(
            "AND opinion_type = :opinion_type",
            params={"opinion_type": opinion_type, "limit": limit},
        ),
        "opinion_type": opinion_type,
        "limit": limit,
    }
