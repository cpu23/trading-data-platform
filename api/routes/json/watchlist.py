from fastapi import APIRouter

from config import load_config

router = APIRouter()


@router.get("/watchlist")
def get_watchlist():
    config = load_config()
    watchlist = config.get("watchlist", {}).get("trading", [])
    return {"instruments": watchlist}