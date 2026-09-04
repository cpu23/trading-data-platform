from fastapi import APIRouter
from price_stream import db_snapshot

from config import load_config

router = APIRouter()


@router.get("/watchlist")
def get_watchlist():
    config = load_config()
    watchlist = config.get("watchlist", {}).get("trading", [])
    return {"instruments": watchlist}


@router.get("/quotes")
def get_quotes():
    config = load_config()
    return db_snapshot(config)
