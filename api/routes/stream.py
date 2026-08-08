import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Mapping

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

import db
from config import load_config

_LAST_EVENT_ID = re.compile(r"[0-9]+")
_ALLOWED_EVENTS = frozenset(
    {"section_changed", "watchlist_changed", "source_health_changed"}
)
_STREAM_COUNTS: dict[str, int] = {}
_STREAM_COUNTS_LOCK = threading.Lock()
stream_router = APIRouter()


def parse_last_event_id(value: str | None) -> int | None:
    """Parse the browser's cursor without accepting signs or whitespace."""
    if value is None:
        return None
    if not _LAST_EVENT_ID.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc


def _valid_event(row: Mapping[str, object]) -> bool:
    event_name = row.get("event_name")
    section_key = row.get("section_key")
    scope_key = row.get("scope_key")
    version = row.get("section_version")
    if (
        not isinstance(event_name, str)
        or event_name not in _ALLOWED_EVENTS
        or not isinstance(section_key, str)
        or not isinstance(scope_key, str)
        or not section_key
        or not scope_key
        or not section_key.strip()
        or not scope_key.strip()
        or "\r" in section_key
        or "\n" in section_key
        or "\r" in scope_key
        or "\n" in scope_key
        or isinstance(version, bool)
    ):
        return False
    try:
        return int(version) > 0 and str(int(version)) == str(version)
    except (TypeError, ValueError, OverflowError):
        return False


def coalesce_ui_events(rows: list[Mapping[str, object]]) -> list[dict[str, object]]:
    """Keep the newest valid wakeup for each invalidation key in id order."""
    latest: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        if not _valid_event(row):
            continue
        event = dict(row)
        key = (event["event_name"], event["section_key"], event["scope_key"])
        latest[key] = event
    return sorted(latest.values(), key=lambda event: int(event["id"]))


def _sse_frame(
    *, event_id: int, event_name: str, event: Mapping[str, object] | None
) -> str:
    if event is None:
        data: dict[str, object] = {}
    else:
        data = {
            "section_key": event["section_key"],
            "scope_key": event["scope_key"],
            "version": int(event["section_version"]),
        }
    return (
        f"id: {event_id}\n"
        f"event: {event_name}\n"
        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
    )


def _sse_settings(config: Mapping[str, object]) -> dict[str, float | int | bool]:
    raw = config.get("event_pipeline", {})
    pipeline = raw if isinstance(raw, Mapping) else {}
    raw_sse = pipeline.get("sse", {})
    sse = raw_sse if isinstance(raw_sse, Mapping) else {}
    return {
        "enabled": bool(sse.get("enabled", False)),
        "heartbeat_seconds": max(
            15.0, min(float(sse.get("heartbeat_seconds", 15)), 30.0)
        ),
        "poll_seconds": max(float(sse.get("poll_seconds", 0.5)), 0.05),
        "replay_limit": max(1, min(int(sse.get("replay_limit", 100)), 100)),
        "max_streams_per_client": max(
            1, min(int(sse.get("max_streams_per_client", 3)), 3)
        ),
        "retention_hours": max(int(sse.get("retention_hours", 48)), 1),
    }


def _claim_stream(host: str, limit: int) -> bool:
    with _STREAM_COUNTS_LOCK:
        count = _STREAM_COUNTS.get(host, 0)
        if count >= limit:
            return False
        _STREAM_COUNTS[host] = count + 1
        return True


def _release_stream(host: str) -> None:
    with _STREAM_COUNTS_LOCK:
        count = _STREAM_COUNTS.get(host, 0)
        if count <= 1:
            _STREAM_COUNTS.pop(host, None)
        else:
            _STREAM_COUNTS[host] = count - 1


def _event_stats() -> tuple[int | None, int | None]:
    rows = db.query_many(
        """
        SELECT MIN(id) AS min_id, MAX(id) AS max_id
        FROM ui_events
        WHERE expires_at > now()
        """,
    )
    if not rows:
        return None, None
    row = rows[0]
    min_id = row.get("min_id")
    max_id = row.get("max_id")
    return (
        int(min_id) if min_id is not None else None,
        int(max_id) if max_id is not None else None,
    )


def _event_rows(cursor: int, limit: int) -> list[dict[str, object]]:
    return db.query_many(
        """
        SELECT id, event_name, section_key, scope_key, section_version
        FROM ui_events
        WHERE expires_at > now() AND id > :cursor
        ORDER BY id ASC
        LIMIT :limit
        """,
        {"cursor": cursor, "limit": limit},
    )


async def stream_ui_events(
    request: Request,
    *,
    config: Mapping[str, object],
    cursor: int,
    host: str,
) -> AsyncIterator[str]:
    settings = _sse_settings(config)
    replay_limit = int(settings["replay_limit"])
    heartbeat_seconds = float(settings["heartbeat_seconds"])
    poll_seconds = float(settings["poll_seconds"])
    resync_sent = False
    last_heartbeat = time.monotonic()
    try:
        while True:
            if await request.is_disconnected():
                return
            min_id, max_id = await run_in_threadpool(_event_stats)
            if max_id is not None:
                gap = min_id is not None and cursor < min_id - 1
                reset = cursor > max_id
                if (gap or reset) and not resync_sent:
                    yield _sse_frame(
                        event_id=max_id, event_name="resync_required", event=None
                    )
                    resync_sent = True
                if gap or reset:
                    cursor = max_id

            rows = await run_in_threadpool(_event_rows, cursor, replay_limit + 1)
            batch = rows[:replay_limit] if len(rows) > replay_limit else rows
            if batch:
                cursor = max(int(event["id"]) for event in batch)
                events = coalesce_ui_events(batch)
                for event in events:
                    yield _sse_frame(
                        event_id=int(event["id"]),
                        event_name=str(event["event_name"]),
                        event=event,
                    )
                if events:
                    last_heartbeat = time.monotonic()
                continue

            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        return
    finally:
        _release_stream(host)


@stream_router.get("/stream", response_class=StreamingResponse)
async def stream(request: Request) -> StreamingResponse:
    config = getattr(request.app.state, "config", None)
    if config is None:
        config = load_config()
    settings = _sse_settings(config)
    if not settings["enabled"]:
        raise HTTPException(status_code=404, detail="SSE stream disabled")
    cursor = parse_last_event_id(request.headers.get("last-event-id"))
    client = request.client
    host = client.host if client is not None else "unknown"
    limit = int(settings["max_streams_per_client"])
    if not _claim_stream(host, limit):
        raise HTTPException(status_code=429, detail="Too many streams")
    initial_cursor = 0 if cursor is None else cursor
    return StreamingResponse(
        stream_ui_events(request, config=config, cursor=initial_cursor, host=host),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
        },
    )
