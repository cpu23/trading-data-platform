import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from api_db import query_many
from fastapi import APIRouter, HTTPException, Query, Request

from config import load_config
from routes.views.cockpit_panels import direction, json_obj, pct_display
from routes.views.dashboard_strip import iso, time_display

router = APIRouter()
MAX_NEWS_FEED_BYTES = 2_000_000
MAX_NEWS_STATE_BYTES = 128_000
MAX_NEWS_ITEMS = 500
STORY_LANES = (
    "market_moving",
    "watchlist_related",
    "macro_central_banks",
    "filings_regulators",
    "developing",
    "low_confidence",
)
STORY_STATES = ("developing", "confirmed", "contradicted", "stale", "closed")
STORY_LANE_LABELS = {
    "market_moving": "Market moving",
    "watchlist_related": "Watchlist related",
    "macro_central_banks": "Macro and central banks",
    "filings_regulators": "Filings and regulators",
    "developing": "Developing",
    "low_confidence": "Low confidence / single source",
}
MAX_STORY_CLUSTERS = 100
MAX_STORY_EVIDENCE = 5
MAX_STORY_CONFIRMATIONS = 20


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


def _story_json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return default
    return value if isinstance(value, type(default)) else default


def _story_time(value) -> str | None:
    if isinstance(value, datetime):
        aware = (
            value.replace(tzinfo=UTC)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(UTC)
        )
        return aware.isoformat()
    if isinstance(value, str):
        return value[:64]
    return None


def _validate_story_filter(value: str | None, allowed: tuple[str, ...], name: str):
    normalized = value.strip().lower() if value else None
    if normalized is not None and normalized not in allowed:
        raise HTTPException(status_code=422, detail=f"Invalid {name}")
    return normalized


def load_story_context(
    *,
    lane: str | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
    fail_soft: bool = True,
) -> dict:
    normalized_lane = _validate_story_filter(lane, STORY_LANES, "lane")
    normalized_state = _validate_story_filter(state, STORY_STATES, "state")
    bounded_limit = max(1, min(MAX_STORY_CLUSTERS, int(limit)))
    bounded_offset = max(0, min(10_000, int(offset)))
    sql = """SELECT c.id, c.canonical_key, c.title, c.summary, c.state, c.lane,
        c.first_seen_at, c.last_seen_at, c.last_material_change_at,
        c.importance, c.novelty, c.confidence, c.entities, c.markets,
        c.source_count, c.version, c.change_summary,
        COALESCE(evidence.members, '[]'::jsonb) AS evidence,
        COALESCE(confirmations.items, '[]'::jsonb) AS market_confirmations
        FROM story_clusters c
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(to_jsonb(member_row) ORDER BY member_row.published_at DESC) AS members
          FROM (
            SELECT source, source_label, title, summary, url, published_at,
                   similarity_score, contribution_type, materially_changed
            FROM story_cluster_members
            WHERE cluster_id = c.id
            ORDER BY published_at DESC, id DESC
            LIMIT 5
          ) member_row
        ) evidence ON TRUE
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(to_jsonb(confirmation_row)
                           ORDER BY confirmation_row.observed_at DESC,
                                    confirmation_row.market_symbol) AS items
          FROM (
            SELECT market_symbol, headline_at, observed_at, pre_headline_move,
                   move_5m, move_30m, move_session, flags, missing_reasons
            FROM story_market_confirmations
            WHERE cluster_id = c.id
            ORDER BY observed_at DESC, market_symbol
            LIMIT 20
          ) confirmation_row
        ) confirmations ON TRUE
        WHERE (:lane IS NULL OR c.lane = :lane)
          AND (:state IS NULL OR c.state = :state)
        ORDER BY c.last_material_change_at DESC, c.id DESC
        LIMIT :limit OFFSET :offset"""
    try:
        rows = query_many(
            sql,
            {
                "lane": normalized_lane,
                "state": normalized_state,
                "limit": bounded_limit,
                "offset": bounded_offset,
            },
        )
    except Exception:
        if not fail_soft:
            raise
        return {
            "status": "unavailable",
            "clusters": [],
            "lanes": {name: [] for name in STORY_LANES},
            "limit": bounded_limit,
            "offset": bounded_offset,
        }
    clusters = []
    for raw in rows:
        evidence = _story_json(raw.get("evidence"), [])[:MAX_STORY_EVIDENCE]
        public_evidence = []
        for member in evidence:
            if not isinstance(member, dict):
                continue
            public_evidence.append(
                {
                    "source": str(member.get("source") or "news")[:64],
                    "source_label": str(
                        member.get("source_label") or member.get("source") or "News"
                    )[:100],
                    "title": str(member.get("title") or "")[:500],
                    "summary": str(member.get("summary") or "")[:1000] or None,
                    "url": _safe_url(member.get("url")),
                    "published_at": _story_time(member.get("published_at")),
                    "similarity_score": member.get("similarity_score"),
                    "contribution_type": str(
                        member.get("contribution_type") or "repeated_coverage"
                    )[:40],
                    "materially_changed": bool(member.get("materially_changed")),
                }
            )
        confirmation_values = _story_json(raw.get("market_confirmations"), [])[
            :MAX_STORY_CONFIRMATIONS
        ]
        public_confirmations = [
            {
                key: (
                    _story_time(item.get(key))
                    if key in {"headline_at", "observed_at"}
                    else item.get(key)
                )
                for key in (
                    "market_symbol",
                    "headline_at",
                    "observed_at",
                    "pre_headline_move",
                    "move_5m",
                    "move_30m",
                    "move_session",
                    "flags",
                    "missing_reasons",
                )
            }
            for item in confirmation_values
            if isinstance(item, dict)
        ]
        entities = _story_json(raw.get("entities"), [])[:50]
        markets = _story_json(raw.get("markets"), [])[:50]
        sources = list(dict.fromkeys(item["source_label"] for item in public_evidence))[
            :MAX_STORY_EVIDENCE
        ]
        cluster = {
            key: raw.get(key)
            for key in (
                "id",
                "canonical_key",
                "title",
                "summary",
                "state",
                "lane",
                "importance",
                "novelty",
                "confidence",
                "source_count",
                "version",
                "change_summary",
            )
        }
        cluster["id"] = str(cluster["id"])
        for key in (
            "first_seen_at",
            "last_seen_at",
            "last_material_change_at",
        ):
            cluster[key] = _story_time(raw.get(key))
        cluster.update(
            entities=entities,
            markets=markets,
            sources=sources,
            evidence=public_evidence,
            market_confirmations=public_confirmations,
        )
        clusters.append(cluster)
    grouped = {
        name: [cluster for cluster in clusters if cluster["lane"] == name]
        for name in STORY_LANES
    }
    return {
        "status": "published" if clusters else "empty",
        "clusters": clusters,
        "lanes": grouped,
        "limit": bounded_limit,
        "offset": bounded_offset,
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


# ---------------------------------------------------------------------------
# Change feed
# ---------------------------------------------------------------------------

CHANGE_FEED_MAX_LIMIT = 50

_STATE_CLASSES = {
    "confirmed": "bullish",
    "contradicted": "bearish",
    "completed": "mixed",
    "developing": "neutral",
}

_CHANGE_FEED_SQL = """
    SELECT routed.event_id, routed.observed_at, routed.effective_at,
           routed.published_at, routed.event_type, routed.source,
           routed.payload, routed.markets, routed.importance, routed.novelty,
           COALESCE(react.windows, '[]'::jsonb) AS reaction_windows,
           COALESCE(conf.flags, '[]'::jsonb) AS confirmation_flags,
           COALESCE(atom.present, false) AS interpretation_available
    FROM (
        SELECT DISTINCT ON (me.id)
               me.id AS event_id, me.observed_at, me.effective_at,
               me.published_at, me.event_type, me.source, me.payload,
               me.markets, em.importance, em.novelty
        FROM market_events me
        JOIN event_materiality em ON em.event_id = me.id
        WHERE em.decision = 'route'
        ORDER BY me.id,
                 CASE WHEN em.job_type = 'event_atom' THEN 0 ELSE 1 END,
                 em.score DESC, em.created_at DESC
    ) routed
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
                   'timeframe', w.timeframe,
                   'horizon', w.horizon,
                   'percentage_move', w.percentage_move,
                   'reaction_state', w.reaction_state
               ) ORDER BY w.timeframe, w.horizon) AS windows
        FROM (
            SELECT timeframe, horizon, percentage_move, reaction_state
            FROM event_reaction_windows
            WHERE event_id = routed.event_id
            ORDER BY timeframe, horizon
            LIMIT 4
        ) w
    ) react ON TRUE
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(DISTINCT flag ORDER BY flag) AS flags
        FROM (
            SELECT jsonb_array_elements_text(smc.flags) AS flag
            FROM story_market_confirmations smc
            WHERE smc.source_event_id = routed.event_id
            LIMIT 25
        ) f
    ) conf ON TRUE
    LEFT JOIN LATERAL (
        SELECT true AS present
        FROM analysis_atoms
        WHERE source_event_id = routed.event_id
          AND status IN ('validated', 'published')
        LIMIT 1
    ) atom ON TRUE
    WHERE (
        :before IS NULL
        OR routed.observed_at < :before
        OR (
            :before_id IS NOT NULL
            AND routed.observed_at = :before
            AND routed.event_id < :before_id
        )
    )
    ORDER BY routed.observed_at DESC, routed.event_id DESC
    LIMIT :limit
"""


def _validate_before(value):
    """Strict ISO-8601 (timezone-aware) validation; 422 before any DB access."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="Invalid before timestamp; expected ISO-8601.",
            ) from exc
    else:
        raise HTTPException(
            status_code=422, detail="Invalid before timestamp; expected ISO-8601."
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTTPException(status_code=422, detail="before must include a UTC offset.")
    return parsed.astimezone(UTC)


def _json_list(value, limit: int) -> list:
    if isinstance(value, list):
        return list(value[:limit])
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed[:limit] if isinstance(parsed, list) else []
    return []


def _importance_label(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    if value >= 0.7:
        return "High"
    if value >= 0.4:
        return "Medium"
    return "Low"


def _importance_class(value) -> str | None:
    label = _importance_label(value)
    return label.lower() if label else None


def _event_state(flags: list[str]) -> str:
    names = set(flags)
    if not names:
        return "developing"
    if "initial_move_reversed" in names:
        return "contradicted"
    if "confirmed_by_market" in names:
        return "confirmed"
    if "no_material_reaction" in names:
        return "completed"
    return "developing"


def _market_symbols(markets, limit: int = 4) -> list[str]:
    symbols: list[str] = []
    items = _json_list(markets, limit=limit)
    for item in items:
        if isinstance(item, dict):
            symbol = item.get("symbol") or item.get("canonical_id")
        else:
            symbol = item
        symbol = str(symbol or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= limit:
            break
    return symbols


def _feed_row(raw: dict) -> dict:
    payload = json_obj(raw.get("payload"))
    event_type = raw.get("event_type") or "event"
    title = payload.get("title") or event_type
    timestamp = (
        raw.get("effective_at") or raw.get("published_at") or raw.get("observed_at")
    )
    windows = []
    for item in _json_list(raw.get("reaction_windows"), limit=4):
        if not isinstance(item, dict):
            continue
        move = item.get("percentage_move")
        windows.append(
            {
                "timeframe": item.get("timeframe"),
                "horizon": item.get("horizon"),
                "percentage_move": move,
                "reaction_state": item.get("reaction_state"),
                "direction": direction(move),
                "display": pct_display(move),
            }
        )
        if len(windows) >= 4:
            break
    flags = [
        str(flag)
        for flag in _json_list(raw.get("confirmation_flags"), limit=25)
        if flag is not None
    ]
    state = _event_state(flags)
    importance = raw.get("importance")
    novelty = raw.get("novelty")
    return {
        "event_id": str(raw.get("event_id") or ""),
        "observed_at": iso(raw.get("observed_at")),
        "effective_at": iso(raw.get("effective_at")),
        "published_at": iso(raw.get("published_at")),
        "time_display": time_display(timestamp),
        "title": str(title)[:500],
        "source": raw.get("source"),
        "markets": _market_symbols(raw.get("markets"), limit=4),
        "importance": importance,
        "importance_label": _importance_label(importance),
        "importance_class": _importance_class(importance),
        "novelty": novelty,
        "novelty_display": (
            f"{novelty:.2f}" if isinstance(novelty, (int, float)) else None
        ),
        "reaction_windows": windows,
        "state": state,
        "state_class": _STATE_CLASSES.get(state, "neutral"),
        "interpretation_available": bool(raw.get("interpretation_available")),
    }


def load_change_feed(
    config: dict, *, before=None, before_id=None, limit: int = 30
) -> dict:
    """Latest routed market events, newest first, bounded by ``limit``.

    ``before`` and ``before_id`` form the full ordering cursor. Both are
    validated before database access. ``limit`` is clamped to
    ``CHANGE_FEED_MAX_LIMIT``; an extra row is fetched to signal
    ``has_earlier`` for the "Load earlier" control.
    """
    bounded_limit = max(1, min(CHANGE_FEED_MAX_LIMIT, int(limit)))
    before_dt = _validate_before(before)
    before_event_id = None
    if before_id is not None:
        if before_dt is None:
            raise HTTPException(status_code=422, detail="before_id requires before.")
        try:
            before_event_id = UUID(str(before_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=422, detail="Invalid before_id; expected UUID."
            ) from exc
    try:
        rows = query_many(
            _CHANGE_FEED_SQL,
            params={
                "before": before_dt,
                "before_id": before_event_id,
                "limit": bounded_limit + 1,
            },
            config=config,
        )
    except Exception:
        return {
            "available": False,
            "rows": [],
            "has_earlier": False,
            "limit": bounded_limit,
            "oldest_observed_at": None,
            "oldest_event_id": None,
        }
    has_earlier = len(rows) > bounded_limit
    feed_rows = [_feed_row(raw) for raw in rows[:bounded_limit]]
    return {
        "available": True,
        "rows": feed_rows,
        "has_earlier": has_earlier,
        "limit": bounded_limit,
        "oldest_observed_at": feed_rows[-1]["observed_at"] if feed_rows else None,
        "oldest_event_id": feed_rows[-1]["event_id"] if feed_rows else None,
    }


@router.get("/partials/news/change-feed")
def partial_news_change_feed(
    request: Request,
    before: str | None = Query(default=None),
    before_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=30, ge=1, le=CHANGE_FEED_MAX_LIMIT),
):
    """Return the canonical continuous material change feed for /news."""
    config = load_config()
    feed = load_change_feed(config, before=before, before_id=before_id, limit=limit)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/change_feed.html",
        {
            "request": request,
            "feed": feed,
            "append": before is not None,
            "news_page": request.url.path == "/partials/news/change-feed",
        },
    )


@router.get("/news")
def news_page(
    request: Request,
    lane: str | None = Query(default=None, max_length=40),
    state: str | None = Query(default=None, max_length=24),
):
    config = load_config()
    selected_lane = _validate_story_filter(lane, STORY_LANES, "lane")
    selected_state = _validate_story_filter(state, STORY_STATES, "state")
    stories = load_story_context(
        lane=selected_lane,
        state=selected_state,
        limit=50,
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "news.html",
        {
            "request": request,
            "stories": stories,
            "source_states": load_source_states(config),
            "story_lanes": STORY_LANE_LABELS,
            "story_states": STORY_STATES,
            "selected_lane": selected_lane,
            "selected_state": selected_state,
        },
    )
