import json

from fastapi import APIRouter, HTTPException

from config import load_config
from db import query_many, query_one

router = APIRouter()


def _payload(row):
    if not row:
        return {}
    value = row.get("payload") or {}
    return json.loads(value) if isinstance(value, str) else value


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


@router.get("/changes/latest")
def get_latest_changes():
    row = query_one(
        "SELECT opinion_id, created_at, payload FROM structured_opinions "
        "WHERE opinion_type='cycle_delta' AND lifecycle_status='published' "
        "ORDER BY published_at DESC LIMIT 1",
        config=load_config(),
    )
    return {
        "opinion_id": str(row["opinion_id"]) if row else None,
        "created_at": row.get("created_at") if row else None,
        **_payload(row),
    }


@router.get("/assets")
def get_assets():
    config = load_config()
    return {
        "assets": [
            item["symbol"] for item in config.get("watchlist", {}).get("trading", [])
        ]
    }


@router.get("/intelligence/current")
def get_current_intelligence():
    row = query_one(
        """
        SELECT opinion_id, correlation_id, baseline_opinion_id, created_at,
               published_at, direction, confidence, summary, payload, data_inputs
        FROM structured_opinions
        WHERE opinion_type = 'narrative_memory'
          AND lifecycle_status = 'published'
        ORDER BY published_at DESC
        LIMIT 1
        """,
        config=load_config(),
    )
    if not row:
        return {"available": False, "intelligence": None}
    return {
        "available": True,
        "intelligence": {
            **row,
            "opinion_id": str(row["opinion_id"]),
            "correlation_id": str(row["correlation_id"])
            if row.get("correlation_id")
            else None,
            "baseline_opinion_id": str(row["baseline_opinion_id"])
            if row.get("baseline_opinion_id")
            else None,
            "payload": _payload(row),
            "data_inputs": _json_object(row.get("data_inputs")),
        },
    }


@router.get("/assets/{symbol}")
def get_asset(symbol: str):
    config = load_config()
    allowed = {
        item["symbol"]: item for item in config.get("watchlist", {}).get("trading", [])
    }
    symbol = symbol.upper()
    if symbol not in allowed:
        raise HTTPException(404, "Unknown asset")
    panel = query_one(
        "SELECT opinion_id, correlation_id, baseline_opinion_id, created_at, "
        "published_at, direction, confidence, summary, payload, data_inputs "
        "FROM structured_opinions WHERE opinion_type='asset_panel' AND scope=:scope "
        "AND lifecycle_status='published' ORDER BY published_at DESC LIMIT 1",
        {"scope": f"asset:{symbol}"},
        config,
    )
    timeline = query_many(
        "SELECT opinion_id, correlation_id, baseline_opinion_id, created_at, "
        "published_at, direction, confidence, summary, payload, data_inputs "
        "FROM structured_opinions WHERE scope=:scope AND lifecycle_status='published' "
        "ORDER BY published_at DESC LIMIT 30",
        {"scope": f"asset:{symbol}"},
        config,
    )
    return {
        "asset": allowed[symbol],
        "panel": {
            **(panel or {}),
            "payload": _payload(panel),
            "data_inputs": _json_object((panel or {}).get("data_inputs")),
        },
        "timeline": [
            {
                **row,
                "payload": _payload(row),
                "data_inputs": _json_object(row.get("data_inputs")),
            }
            for row in timeline
        ],
    }
