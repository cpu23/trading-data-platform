from datetime import date

from fastapi import APIRouter, HTTPException

from config import load_config
from db import query_one
from staleness import get_staleness_config, is_stale

router = APIRouter()


@router.get("/briefing/latest")
def get_briefing_latest():
    config = load_config()
    thresholds = get_staleness_config(config)

    sql = """
        SELECT briefing_id, briefing_date, created_at, content, sections,
               model_used, prompt_version, opinion_ids
        FROM daily_briefings
        WHERE lifecycle_status = 'published'
        ORDER BY briefing_date DESC, created_at DESC
        LIMIT 1
    """
    row = query_one(sql, config=config)
    if row is None:
        raise HTTPException(status_code=404, detail="No briefing found", headers={"X-Error-Code": "NOT_FOUND"})

    stale, stale_reason = is_stale(row["created_at"], thresholds["briefing_hours"])

    return {
        "briefing_id": str(row["briefing_id"]),
        "briefing_date": row["briefing_date"].isoformat() if isinstance(row["briefing_date"], date) else str(row["briefing_date"]),
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
        "stale": stale,
        "stale_reason": stale_reason,
        "sections": row["sections"] if row["sections"] is not None else {},
        "model_used": row["model_used"],
        "prompt_version": row["prompt_version"],
        "opinion_ids": [str(value) for value in (row.get("opinion_ids") or [])],
    }


@router.get("/briefing/{briefing_date}")
def get_briefing_by_date(briefing_date: str):
    config = load_config()
    thresholds = get_staleness_config(config)

    sql = """
        SELECT briefing_id, briefing_date, created_at, content, sections,
               model_used, prompt_version, opinion_ids
        FROM daily_briefings
        WHERE briefing_date = :date
          AND lifecycle_status = 'published'
        ORDER BY created_at DESC
        LIMIT 1
    """
    row = query_one(sql, params={"date": briefing_date}, config=config)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No briefing found for {briefing_date}", headers={"X-Error-Code": "NOT_FOUND"})

    stale, stale_reason = is_stale(row["created_at"], thresholds["briefing_hours"])

    return {
        "briefing_id": str(row["briefing_id"]),
        "briefing_date": row["briefing_date"].isoformat() if isinstance(row["briefing_date"], date) else str(row["briefing_date"]),
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
        "stale": stale,
        "stale_reason": stale_reason,
        "sections": row["sections"] if row["sections"] is not None else {},
        "model_used": row["model_used"],
        "prompt_version": row["prompt_version"],
        "opinion_ids": [str(value) for value in (row.get("opinion_ids") or [])],
    }
