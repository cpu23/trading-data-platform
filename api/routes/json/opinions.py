from fastapi import APIRouter, Query

from config import load_config
from db import query_many

router = APIRouter()


@router.get("/opinions/latest")
def get_opinions_latest(limit: int = Query(default=20, ge=1, le=200)):
    config = load_config()

    sql = """
        SELECT opinion_id, created_at, opinion_type, scope, direction,
               confidence, timeframe, summary, model_used, prompt_version
        FROM structured_opinions
        WHERE lifecycle_status = 'published'
        ORDER BY created_at DESC
        LIMIT :limit
    """
    rows = query_many(sql, params={"limit": limit}, config=config)

    return {
        "opinions": [
            {
                "opinion_id": str(row["opinion_id"]),
                "created_at": row["created_at"].isoformat()
                if hasattr(row["created_at"], "isoformat")
                else str(row["created_at"]),
                "opinion_type": row["opinion_type"],
                "scope": row["scope"],
                "direction": row.get("direction"),
                "confidence": row.get("confidence"),
                "timeframe": row.get("timeframe"),
                "summary": row.get("summary"),
                "model_used": row.get("model_used"),
                "prompt_version": row.get("prompt_version"),
            }
            for row in rows
        ],
        "limit": limit,
    }


@router.get("/opinions/{opinion_type}")
def get_opinions_by_type(
    opinion_type: str, limit: int = Query(default=20, ge=1, le=200)
):
    config = load_config()

    sql = """
        SELECT opinion_id, created_at, opinion_type, scope, direction,
               confidence, timeframe, summary, model_used, prompt_version
        FROM structured_opinions
        WHERE opinion_type = :opinion_type
          AND lifecycle_status = 'published'
        ORDER BY created_at DESC
        LIMIT :limit
    """
    rows = query_many(
        sql, params={"opinion_type": opinion_type, "limit": limit}, config=config
    )

    return {
        "opinions": [
            {
                "opinion_id": str(row["opinion_id"]),
                "created_at": row["created_at"].isoformat()
                if hasattr(row["created_at"], "isoformat")
                else str(row["created_at"]),
                "opinion_type": row["opinion_type"],
                "scope": row["scope"],
                "direction": row.get("direction"),
                "confidence": row.get("confidence"),
                "timeframe": row.get("timeframe"),
                "summary": row.get("summary"),
                "model_used": row.get("model_used"),
                "prompt_version": row.get("prompt_version"),
            }
            for row in rows
        ],
        "opinion_type": opinion_type,
        "limit": limit,
    }
