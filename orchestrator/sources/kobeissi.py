"""Kobeissi Letter tweet fetcher — normalises tweets into feed items."""

from __future__ import annotations

import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from http_client import get_shared_client, make_request
from logging_config import get_logger
from provider_origins import validate_configured_origin
from sources.news_result import NewsCollectionResult, NewsPublication
from sources.news_storage import atomic_write_json, read_json

logger = get_logger("kobeissi")

MAX_KOBEISSI_BYTES = 5_000_000
KOBEISSI_DEADLINE_SECONDS = 60.0


def _normalise_tweet(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw twitterapi.io tweet into a feed item."""
    entities = raw.get("entities", {})
    symbols = [s.get("text", "") for s in entities.get("symbols", [])]
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
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def _fetch_bytes(
    url: str, *, headers: dict | None = None, timeout: float = 30.0
) -> bytes:
    """Fetch a Kobeissi response body through the shared resolve-and-pin
    transport: the origin is validated up front, every send re-resolves DNS
    and pins the connection (a poisoned/rebound api.twitterapi.io cannot
    reach private networks), redirects are rejected so the X-API-Key
    credential never follows a Location to another origin (a 3xx fails
    closed), and body size and total fetch time are bounded. Tests inject
    fake fetchers here to exercise normalisation.
    """
    resp = make_request(
        "GET",
        url,
        headers=headers,
        timeout=timeout,
        max_retries=1,
        follow_redirects=False,
        client=get_shared_client(),
        deadline_seconds=KOBEISSI_DEADLINE_SECONDS,
        max_response_bytes=MAX_KOBEISSI_BYTES,
    )
    if resp.is_redirect or resp.status_code in {301, 302, 303, 307, 308}:
        raise ValueError(
            f"Kobeissi upstream redirected (HTTP {resp.status_code}); redirects are rejected"
        )
    resp.raise_for_status()
    return resp.content


def run_kobeissi(config: dict, count: int = 20) -> NewsCollectionResult:
    """
    Fetch recent Kobeissi Letter tweets via twitterapi.io.

    Returns a typed outcome that distinguishes empty success from failure.
    """
    kobeissi_config = config.get("kobeissi", {})
    api_key = kobeissi_config.get("api_key", "")
    user_id = kobeissi_config.get("user_id", "3316376038")
    api_base = validate_configured_origin(
        kobeissi_config.get("api_base", "https://api.twitterapi.io"),
        kobeissi_config,
        label="Kobeissi api_base",
        canonical={"https://api.twitterapi.io"},
    )
    state_path = Path(kobeissi_config.get("state_path", "var/news/kobeissi/state.json"))
    output_dir = Path(kobeissi_config.get("output_path", "var/news/kobeissi"))

    if not api_key:
        logger.error("kobeissi_no_api_key")
        raise ValueError("TWITTERAPI_KEY is required to collect Kobeissi news")

    state: dict[str, Any] = read_json(
        state_path, {"last_seen_id": None, "last_poll": None}
    )
    if not isinstance(state, dict):
        state = {"last_seen_id": None, "last_poll": None}
    since_id = state.get("last_seen_id")

    params = {"userId": user_id, "count": str(count)}
    url = f"{api_base}/twitter/user/tweet_timeline?{urllib.parse.urlencode(params)}"

    logger.info("kobeissi_fetch_started", count=count)
    # Fetched through the shared resolve-and-pin transport (see
    # ``_fetch_bytes``): the origin is validated and every send re-resolves
    # DNS and pins the connection, redirects are disabled so the X-API-Key
    # credential never follows a Location (a 3xx fails closed), and the
    # body and total fetch time are bounded.
    try:
        data = json.loads(
            _fetch_bytes(
                url,
                headers={
                    "X-API-Key": api_key,
                    "User-Agent": "TradingResearchSystem/1.0",
                },
            )
        )
    except Exception as exc:
        error = f"Kobeissi fetch failed: {type(exc).__name__}"
        logger.error("kobeissi_fetch_failed", error=error)
        state.update(
            {
                "last_poll": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": error,
            }
        )
        atomic_write_json(state_path, state)
        return NewsCollectionResult([], "error", error, error_class="transient_source")

    if not isinstance(data, dict) or data.get("status") != "success":
        error = "Kobeissi upstream API returned an error"
        logger.error("kobeissi_api_error", error=error)
        state.update(
            {
                "last_poll": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": error,
            }
        )
        atomic_write_json(state_path, state)
        return NewsCollectionResult([], "error", error, error_class="transient_source")

    response_data = data.get("data")
    raw_tweets = (
        response_data.get("tweets") if isinstance(response_data, dict) else None
    )
    if not isinstance(raw_tweets, list):
        error = "Kobeissi upstream API returned an invalid response"
        logger.error("kobeissi_api_error", error=error)
        state.update(
            {
                "last_poll": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": error,
            }
        )
        atomic_write_json(state_path, state)
        return NewsCollectionResult(
            [], "error", error, error_class="invalid_source_data"
        )

    new_items: list[dict[str, Any]] = []
    try:
        for raw in raw_tweets:
            if not isinstance(raw, dict):
                raise TypeError("tweet entry must be an object")
            if not isinstance(raw.get("id"), (str, int)) or not str(raw["id"]).strip():
                raise ValueError("tweet id is required")
            if not isinstance(raw.get("text"), str):
                raise TypeError("tweet text must be a string")
            try:
                already_seen = since_id is not None and int(raw["id"]) <= int(since_id)
            except (TypeError, ValueError):
                already_seen = since_id is not None and str(raw.get("id", "")) == str(
                    since_id
                )
            if already_seen:
                break
            new_items.append(_normalise_tweet(raw))
    except Exception as exc:
        error = (
            f"Kobeissi upstream API returned an invalid response: {type(exc).__name__}"
        )
        logger.error("kobeissi_api_error", error=error)
        state.update(
            {
                "last_poll": datetime.now(UTC).isoformat(),
                "status": "error",
                "error": error,
            }
        )
        atomic_write_json(state_path, state)
        return NewsCollectionResult(
            [], "error", error, error_class="invalid_source_data"
        )

    candidate_state = dict(state)
    if new_items:
        candidate_state["last_seen_id"] = new_items[0]["id"].split(":")[1]
    candidate_state["last_poll"] = datetime.now(UTC).isoformat()
    candidate_state["status"] = "ok"
    candidate_state["error"] = None
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    publication = NewsPublication(
        snapshot_path=output_dir / f"kobeissi_{today}.json",
        state_path=state_path,
        candidate_state=candidate_state,
    )

    if new_items:
        logger.info("kobeissi_fetch_complete", new_items=len(new_items))
    else:
        logger.info("kobeissi_fetch_complete", new_items=0)

    return NewsCollectionResult(new_items, "ok", publication=publication)
