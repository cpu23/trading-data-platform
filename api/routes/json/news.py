"""Bounded News feed and source-state JSON endpoints."""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

import config as app_config
from routes.views.news import (
    MAX_STORY_CLUSTERS,
    load_source_states,
    load_story_context,
)

router = APIRouter(tags=["news"])


@router.get("/news/clusters")
def get_news_clusters(
    lane: str | None = Query(default=None, max_length=40),
    state: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=50, ge=1, le=MAX_STORY_CLUSTERS),
    offset: int = Query(default=0, ge=0, le=10_000),
):
    """Return bounded canonical stories with evidence and observations."""
    payload = load_story_context(
        lane=lane,
        state=state,
        limit=limit,
        offset=offset,
    )
    if payload["status"] == "unavailable":
        return JSONResponse(
            {"error": "Canonical news stories are temporarily unavailable."},
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
        sources.append(
            {
                **state,
                "enabled": src_config.get("enabled", False),
                "on_demand_only": src_config.get("on_demand_only", True),
            }
        )
    return JSONResponse({"sources": sources})
