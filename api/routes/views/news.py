import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Request

from config import load_config

router = APIRouter()
MAX_NEWS_FEED_BYTES = 2_000_000
MAX_NEWS_STATE_BYTES = 128_000
MAX_NEWS_ITEMS = 500


def _read_json_bounded(path: Path, max_bytes: int):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _bounded_list(value, *, count: int = 12, width: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()[:width]
        for item in value[:count]
        if isinstance(item, str) and item.strip()
    ]


def _safe_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def load_news_context(config: dict, limit: int = MAX_NEWS_ITEMS) -> dict:
    output = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
    feed_path = output / "feed.json"
    if not feed_path.is_file():
        return {"status": "not_published", "items": [], "generated_at": None}
    payload = _read_json_bounded(feed_path, MAX_NEWS_FEED_BYTES)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return {"status": "invalid", "items": [], "generated_at": None}
    items = []
    safe_limit = max(0, min(int(limit), MAX_NEWS_ITEMS))
    for item in payload["items"][:MAX_NEWS_ITEMS]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("title"), str)
            or not item["title"].strip()
        ):
            continue
        source_id = str(item.get("source") or "news").strip().lower()[:32]
        items.append(
            {
                "title": item["title"].strip()[:240],
                "source": str(
                    item.get("source_label") or item.get("source") or "News"
                ).strip()[:64],
                "source_id": source_id,
                "published": item.get("published", "")[:64]
                if isinstance(item.get("published"), str)
                else None,
                "summary": item.get("summary", "")[:500]
                if isinstance(item.get("summary"), str)
                else "",
                "symbols": _bounded_list(item.get("symbols")),
                "tags": _bounded_list(item.get("tags")),
                "url": _safe_url(item.get("url")),
            }
        )
        if len(items) == safe_limit:
            break
    generated = payload.get("generated_at")
    return {
        "status": "published",
        "items": items,
        "generated_at": generated[:64] if isinstance(generated, str) else None,
    }


def load_source_states(config: dict) -> list[dict]:
    output = Path(
        config.get("news_feed", {}).get("output_path", "/var/lib/trading-data/news")
    )
    states = []
    for name in ("reuters", "kobeissi"):
        state_path = output / name / "state.json"
        state = _read_json_bounded(state_path, MAX_NEWS_STATE_BYTES)
        if state_path.is_file() and not isinstance(state, dict):
            state = {"status": "error", "error": "state file is invalid"}
        elif not isinstance(state, dict):
            state = {}
        error = state.get("error")
        states.append(
            {
                "name": name,
                "enabled": bool(config.get(name, {}).get("enabled", False)),
                "status": str(state.get("status") or "never_polled")[:32],
                "last_poll": str(state.get("last_poll") or "")[:64] or None,
                "error": str(error)[:240] if error else None,
            }
        )
    return states


@router.get("/news")
def news_page(
    request: Request,
    source: str | None = Query(default=None, max_length=32),
    symbol: str | None = Query(default=None, max_length=32),
):
    config = load_config()
    context = load_news_context(config)
    all_items = context["items"]
    sources = sorted({item["source_id"] for item in all_items})
    symbols = sorted(
        {value for item in all_items for value in item["symbols"] + item["tags"]}
    )
    selected_source = source.strip().lower() if source else None
    selected_symbol = symbol.strip().lower() if symbol else None
    filtered = [
        item
        for item in all_items
        if (not selected_source or item["source_id"] == selected_source)
        and (
            not selected_symbol
            or selected_symbol
            in {value.lower() for value in item["symbols"] + item["tags"]}
        )
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "news.html",
        {
            "request": request,
            "news": {**context, "items": filtered},
            "source_states": load_source_states(config),
            "sources": sources,
            "symbols": symbols,
            "selected_source": selected_source,
            "selected_symbol": symbol or "",
        },
    )
