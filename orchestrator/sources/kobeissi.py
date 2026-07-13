"""Kobeissi Letter tweet fetcher — normalises tweets into feed items."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_config import get_logger
from sources.news_storage import atomic_write_json, merge_items, read_json

logger = get_logger("kobeissi")


def _normalise_tweet(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw twitterapi.io tweet into a feed item."""
    entities = raw.get("entities", {})
    symbols = [s.get("text", "") for s in entities.get("symbols", [])]
    urls = [u.get("expanded_url", u.get("url", "")) for u in entities.get("urls", [])]
    hashtags = [h.get("text", "") for h in entities.get("hashtags", [])]

    media = []
    for m in raw.get("extendedEntities", {}).get("media", []):
        media.append({"type": m.get("type", ""), "url": m.get("media_url_https", "")})

    text = raw.get("text", "")
    title = text.split("\n")[0].strip()
    if len(title) > 120:
        title = title[:117] + "..."

    pub = raw.get("createdAt", "")
    try:
        pub = datetime.strptime(pub, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except (ValueError, TypeError):
        pass

    return {
        "id": f"kobeissi:{raw.get('id', '')}",
        "source": "kobeissi",
        "source_label": "Kobeissi Letter",
        "title": title,
        "summary": text,
        "url": raw.get("url", ""),
        "published": pub,
        "symbols": symbols,
        "tags": hashtags,
        "engagement": {
            "views": raw.get("viewCount", 0),
            "likes": raw.get("likeCount", 0),
            "retweets": raw.get("retweetCount", 0),
            "bookmarks": raw.get("bookmarkCount", 0),
        },
        "media": media,
        "meta": {},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def run_kobeissi(config: dict, count: int = 20) -> list[dict[str, Any]]:
    """
    Fetch recent Kobeissi Letter tweets via twitterapi.io.

    Returns normalised feed items.
    """
    kobeissi_config = config.get("kobeissi", {})
    api_key = kobeissi_config.get("api_key", "")
    user_id = kobeissi_config.get("user_id", "3316376038")
    api_base = kobeissi_config.get("api_base", "https://api.twitterapi.io")
    state_path = Path(kobeissi_config.get("state_path", "var/news/kobeissi/state.json"))
    output_dir = Path(kobeissi_config.get("output_path", "var/news/kobeissi"))

    if not api_key:
        logger.error("kobeissi_no_api_key")
        raise ValueError("TWITTERAPI_KEY is required to collect Kobeissi news")

    state = read_json(state_path, {"last_seen_id": None, "last_poll": None})
    if not isinstance(state, dict):
        state = {"last_seen_id": None, "last_poll": None}
    since_id = state.get("last_seen_id")

    params = {"userId": user_id, "count": str(count)}
    url = f"{api_base}/twitter/user/tweet_timeline?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "X-API-Key": api_key,
        "User-Agent": "TradingResearchSystem/1.0",
    })

    logger.info("kobeissi_fetch_started", count=count)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        logger.error("kobeissi_fetch_failed", error=str(e))
        return []

    if data.get("status") != "success":
        logger.error("kobeissi_api_error", msg=data.get("msg", "unknown"))
        return []

    raw_tweets = data.get("data", {}).get("tweets", [])
    if not raw_tweets:
        logger.info("kobeissi_no_tweets")
        return []

    new_items: list[dict[str, Any]] = []
    for raw in raw_tweets:
        try:
            already_seen = since_id is not None and int(raw["id"]) <= int(since_id)
        except (TypeError, ValueError):
            already_seen = since_id is not None and str(raw.get("id", "")) == str(since_id)
        if already_seen:
            break
        new_items.append(_normalise_tweet(raw))

    if new_items:
        state["last_seen_id"] = new_items[0]["id"].split(":")[1]
    state["last_poll"] = datetime.now(timezone.utc).isoformat()
    state["status"] = "ok"
    state["error"] = None
    atomic_write_json(state_path, state)

    if new_items:
        output_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_file = output_dir / f"kobeissi_{today}.json"
        merge_items(daily_file, new_items)
        logger.info("kobeissi_fetch_complete", new_items=len(new_items))
    else:
        logger.info("kobeissi_fetch_complete", new_items=0)

    return new_items
