import json

from fastapi import APIRouter, HTTPException
from config import load_config
from db import query_many, query_one

router = APIRouter()


def _payload(row):
    if not row: return {}
    value = row.get("payload") or {}
    return json.loads(value) if isinstance(value, str) else value


@router.get("/changes/latest")
def get_latest_changes():
    row = query_one(
        "SELECT opinion_id, created_at, payload FROM structured_opinions "
        "WHERE opinion_type='cycle_delta' AND lifecycle_status='published' "
        "ORDER BY published_at DESC LIMIT 1", config=load_config(),
    )
    return {"opinion_id": str(row["opinion_id"]) if row else None, "created_at": row.get("created_at") if row else None, **_payload(row)}


@router.get("/assets")
def get_assets():
    config = load_config()
    return {"assets": [item["symbol"] for item in config.get("watchlist", {}).get("trading", [])]}


@router.get("/assets/{symbol}")
def get_asset(symbol: str):
    config = load_config()
    allowed = {item["symbol"]: item for item in config.get("watchlist", {}).get("trading", [])}
    symbol = symbol.upper()
    if symbol not in allowed: raise HTTPException(404, "Unknown asset")
    panel = query_one(
        "SELECT opinion_id, created_at, direction, confidence, summary, payload "
        "FROM structured_opinions WHERE opinion_type='asset_panel' AND scope=:scope "
        "AND lifecycle_status='published' ORDER BY published_at DESC LIMIT 1",
        {"scope": f"asset:{symbol}"}, config,
    )
    timeline = query_many(
        "SELECT opinion_id, created_at, direction, confidence, summary, payload "
        "FROM structured_opinions WHERE scope=:scope AND lifecycle_status='published' "
        "ORDER BY published_at DESC LIMIT 30",
        {"scope": f"asset:{symbol}"}, config,
    )
    return {
        "asset": allowed[symbol], "panel": {**(panel or {}), "payload": _payload(panel)},
        "timeline": [{**row, "payload": _payload(row)} for row in timeline],
    }
