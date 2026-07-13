"""News feed JSON endpoint — serves the unified feed.json."""
from pathlib import Path
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["news"])


@router.get("/news/feed")
def get_news_feed():
    """Return the unified news feed."""
    from config import load_config
    config = load_config()
    feed_path = Path(
        config.get("news_feed", {}).get("output_path", "var/news")
    ) / "feed.json"

    if not feed_path.exists():
        return JSONResponse(
            {"error": "Feed not generated yet. Run `python cli.py news all` first."},
            status_code=404,
        )

    try:
        payload = json.loads(feed_path.read_text())
    except (OSError, json.JSONDecodeError):
        return JSONResponse({"error": "News feed is temporarily unavailable."}, status_code=503)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return JSONResponse({"error": "News feed is invalid."}, status_code=503)
    return JSONResponse(payload)


@router.get("/news/sources")
def get_news_sources():
    """List available news sources and their last poll status."""

    from config import load_config
    config = load_config()
    output_base = Path(config.get("news_feed", {}).get("output_path", "var/news"))
    sources = []

    for name in ("reuters", "kobeissi"):
        state_file = output_base / name / "state.json"
        src_config = config.get(name, {})
        state = {}
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
            except (OSError, json.JSONDecodeError):
                state = {"status": "error", "error": "state file is invalid"}
            if not isinstance(state, dict):
                state = {"status": "error", "error": "state file is invalid"}

        sources.append({
            "name": name,
            "enabled": src_config.get("enabled", False),
            "last_poll": state.get("last_poll"),
            "status": state.get("status", "never_polled"),
            "error": state.get("error"),
            "on_demand_only": src_config.get("on_demand_only", True),
        })

    return JSONResponse({"sources": sources})
