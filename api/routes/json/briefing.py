from fastapi import APIRouter, HTTPException

from config import load_config
from db import query_one
from serializers import isoformat
from staleness import get_staleness_config, is_stale

router = APIRouter()


def _serialize_briefing(row: dict, thresholds: dict) -> dict:
    stale, stale_reason = is_stale(row["created_at"], thresholds["briefing_hours"])
    return {
        "briefing_id": str(row["briefing_id"]),
        "briefing_date": isoformat(row["briefing_date"]),
        "created_at": isoformat(row["created_at"]),
        "stale": stale,
        "stale_reason": stale_reason,
        "sections": row["sections"] if row["sections"] is not None else {},
        "model_used": row["model_used"],
        "prompt_version": row["prompt_version"],
        "opinion_ids": [str(value) for value in (row.get("opinion_ids") or [])],
    }


def _fetch_briefing(
    order_and_where: str, params: dict | None = None
) -> tuple[dict | None, dict]:
    config = load_config()
    thresholds = get_staleness_config(config)
    sql = f"""
        SELECT briefing_id, briefing_date, created_at, content, sections,
               model_used, prompt_version, opinion_ids
        FROM daily_briefings
        WHERE lifecycle_status = 'published'
        {order_and_where}
    """
    row = query_one(sql, params=params, config=config)
    return row, thresholds


@router.get("/briefing/latest")
def get_briefing_latest():
    row, thresholds = _fetch_briefing(
        "ORDER BY briefing_date DESC, created_at DESC LIMIT 1"
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No briefing found",
            headers={"X-Error-Code": "NOT_FOUND"},
        )
    return _serialize_briefing(row, thresholds)


@router.get("/briefing/{briefing_date}")
def get_briefing_by_date(briefing_date: str):
    row, thresholds = _fetch_briefing(
        "AND briefing_date = :date ORDER BY created_at DESC LIMIT 1",
        params={"date": briefing_date},
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No briefing found for {briefing_date}",
            headers={"X-Error-Code": "NOT_FOUND"},
        )
    return _serialize_briefing(row, thresholds)
