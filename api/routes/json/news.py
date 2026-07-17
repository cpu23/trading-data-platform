"""Bounded News feed and source-state JSON endpoints."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config as app_config
from routes.views.news import load_news_context, load_source_states

router = APIRouter(tags=["news"])


@router.get("/news/feed")
def get_news_feed():
    """Return the bounded, normalized unified News feed."""
    config = app_config.load_config()
    feed_path = Path(
        config.get("news_feed", {}).get("output_path", "var/news")
    ) / "feed.json"
    if not feed_path.exists():
        return JSONResponse(
            {"error": "Feed not generated yet. Run `python cli.py news all` first."},
            status_code=404,
        )

    payload = load_news_context(config)
    if payload["status"] != "published":
        return JSONResponse(
            {"error": "News feed is temporarily unavailable."},
            status_code=503,
        )
    return JSONResponse(payload)


@router.get("/news/sources")
def get_news_sources():
    """List bounded source state without exposing raw provider data."""
    config = app_config.load_config()
    sources = []
    for state in load_source_states(config):
        name = state["name"]
        src_config = config.get(name, {})
        sources.append({
            **state,
            "enabled": src_config.get("enabled", False),
            "on_demand_only": src_config.get("on_demand_only", True),
        })
    return JSONResponse({"sources": sources})
