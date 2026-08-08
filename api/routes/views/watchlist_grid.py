"""Phase 8 cockpit: watchlist grid and asset drawer.

Server-rendered, keyboard-usable watchlist table with price moves,
volatility state, current catalysts, interpretation and freshness, plus an
asset drawer with an intraday chart, macro exposures, analysis atoms and an
operator-notes placeholder.

All SQL is bounded and parameterized. Query params are validated before any
database access; loader failures are fail-soft at call sites (the grid
degrades to ``{"available": False}`` and the drawer route answers 503 with a
generic message, never leaking SQL or exception text).
"""

import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from starlette.requests import Request as StarletteRequest

import config as app_config
from db import query_many, query_one
from logging_config import get_logger
from routes.json.atoms import load_atom_context
from routes.json.events import get_events_upcoming_data
from routes.json.macro import get_macro_dashboard
from routes.views.asset_rules import ASSET_EVENT_RULES

logger = get_logger("watchlist_grid")

router = APIRouter()

VIEW_ALLOWLIST = (
    "fx",
    "rates",
    "indices",
    "commodities",
    "watchlist",
    "event_sensitive",
)
SORT_ALLOWLIST = ("symbol", "last", "chg_5m", "chg_day", "freshness")
DIRECTION_ALLOWLIST = ("asc", "desc")
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,12}$")

STATIC_VIEW_SYMBOLS = {
    "fx": {"EURUSD", "USDJPY", "AUDJPY", "GBPUSD"},
    "rates": {"US10Y", "US02Y", "DE10Y"},
    "indices": {"SP500", "GER40", "UK100"},
    "commodities": {"XAUUSD", "XPTUSD", "WTICOUSD"},
}

MAX_GRID_LIMIT = 200  # hard cap on symbols fetched per grid render
OPINION_FETCH_LIMIT = 300
CATALYST_FETCH_LIMIT = 30
RELATED_EVENTS_FETCH_LIMIT = 30
DRAWER_BUCKETS = 96
EVENT_LOOKBACK_DAYS = 14
DRAWER_ATOMS_LIMIT = 5

_MACRO_CATEGORIES_BY_CURRENCY = {
    "USD": {"usd", "rates", "yield_curve", "inflation", "credit"},
    "EUR": {"rates", "yield_curve", "inflation", "credit"},
    "GBP": {"rates", "yield_curve", "inflation"},
    "JPY": {"rates", "yield_curve", "inflation"},
    "AUD": {"rates", "yield_curve", "inflation"},
}
_KEYWORD_CATEGORIES = {
    "inflation": "inflation",
    "cpi": "inflation",
    "ppi": "inflation",
    "rates": "rates",
    "rate": "rates",
    "fed": "rates",
    "fomc": "rates",
    "yield": "yield_curve",
    "credit": "credit",
}

# --------------------------------------------------------------------------
# Validation and symbol resolution
# --------------------------------------------------------------------------


def _validate_grid_params(view, sort, direction) -> tuple[str, str, str]:
    view_norm = view.strip().lower() if isinstance(view, str) else ""
    sort_norm = sort.strip().lower() if isinstance(sort, str) else ""
    direction_norm = direction.strip().lower() if isinstance(direction, str) else ""
    if view_norm not in VIEW_ALLOWLIST:
        raise ValueError(f"unsupported view: {view}")
    if sort_norm not in SORT_ALLOWLIST:
        raise ValueError(f"unsupported sort: {sort}")
    if direction_norm not in DIRECTION_ALLOWLIST:
        raise ValueError(f"unsupported direction: {direction}")
    return view_norm, sort_norm, direction_norm


def _market_symbol(value: str) -> str:
    """Map a config symbol (e.g. EUR_USD) to its market_data symbol (EURUSD)."""
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def _watchlist_symbols(config: dict) -> set[str]:
    symbols: set[str] = set()
    groups = config.get("watchlist")
    if not isinstance(groups, dict):
        return symbols
    for group in groups.values():
        if isinstance(group, dict):
            for nested in group.get("watchlists") or []:
                if isinstance(nested, dict):
                    for value in nested.get("symbols") or []:
                        if isinstance(value, str):
                            symbols.add(_market_symbol(value))
        elif isinstance(group, list):
            for item in group:
                if isinstance(item, dict):
                    value = item.get("symbol")
                    if isinstance(value, str):
                        symbols.add(_market_symbol(value))
                elif isinstance(item, str):
                    symbols.add(_market_symbol(item))
    return symbols


def _event_sensitive_symbols() -> set[str]:
    return {symbol for symbol in ASSET_EVENT_RULES if SYMBOL_PATTERN.match(symbol)}


def _view_symbols(config: dict, view: str) -> set[str]:
    if view in STATIC_VIEW_SYMBOLS:
        return set(STATIC_VIEW_SYMBOLS[view])
    if view == "watchlist":
        return _watchlist_symbols(config)
    if view == "event_sensitive":
        return _event_sensitive_symbols()
    return set()


def _known_symbols(config: dict) -> set[str]:
    known: set[str] = set()
    for symbols in STATIC_VIEW_SYMBOLS.values():
        known |= symbols
    known |= _watchlist_symbols(config)
    known |= _event_sensitive_symbols()
    return known


def _validate_symbol(symbol) -> str:
    if not isinstance(symbol, str):
        raise ValueError("invalid symbol")
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(normalized):
        raise ValueError("invalid symbol")
    return normalized


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value) -> str | None:
    parsed = _as_utc(value)
    return parsed.isoformat() if parsed else None


def _age_minutes(value, now: datetime) -> int | None:
    parsed = _as_utc(value)
    if parsed is None:
        return None
    return max(0, int((now - parsed).total_seconds() // 60))


def _age_text(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _pct_change(latest, baseline):
    if latest is None or baseline is None or baseline == 0:
        return None
    return round((latest - baseline) / baseline * 100, 2)


def _pct_text(value) -> str | None:
    if value is None:
        return None
    return f"{value:+.2f}%"


def _bounded_strings(value, *, count: int = 8, width: int = 400) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()[:width]
        for item in value[:count]
        if isinstance(item, str) and item.strip()
    ]


# --------------------------------------------------------------------------
# Interpretation labels (never action words)
# --------------------------------------------------------------------------


def _direction_label(direction) -> str:
    value = str(direction or "").strip().lower()
    if not value:
        return "neutral"
    if "bullish" in value or value in {"up", "higher", "positive"}:
        return "bullish"
    if "bearish" in value or value in {"down", "lower", "negative"}:
        return "bearish"
    if "mixed" in value or value == "balanced":
        return "mixed"
    return "neutral"


_ATOM_BULLISH_MARKERS = (
    "bullish",
    "bull ",
    "upside",
    "strengthen",
    "improving",
    "risk-on",
    "risk_on",
)
_ATOM_BEARISH_MARKERS = (
    "bearish",
    "bear ",
    "downside",
    "weaken",
    "deteriorat",
    "risk-off",
    "risk_off",
)
_ATOM_MIXED_MARKERS = ("mixed", "conflicting", "divergence", "balanced")


def _atom_interpretation(atom: dict) -> str:
    text = " ".join(
        str(atom.get(key) or "")
        for key in ("claim", "interpretation_text", "observation_text")
    ).casefold()
    if any(marker in text for marker in _ATOM_MIXED_MARKERS):
        return "mixed"
    if any(marker in text for marker in _ATOM_BEARISH_MARKERS):
        return "bearish"
    if any(marker in text for marker in _ATOM_BULLISH_MARKERS):
        return "bullish"
    return "neutral"


def _interpretation_class(label: str) -> str:
    if label == "bullish":
        return "story-state story-state-confirmed"
    if label == "bearish":
        return "story-state story-state-contradicted"
    return "story-state"


# --------------------------------------------------------------------------
# Volatility and event matching
# --------------------------------------------------------------------------


def _vol_state(recent_range, prior_range) -> str | None:
    if recent_range is None or prior_range is None:
        return None
    try:
        recent = float(recent_range)
        prior = float(prior_range)
    except (TypeError, ValueError):
        return None
    if prior <= 0:
        return "normal"
    ratio = recent / prior
    if ratio >= 1.5:
        return "elevated"
    if ratio <= 0.66:
        return "quiet"
    return "normal"


def _econ_event_matches_asset(symbol: str, event: dict) -> bool:
    rules = ASSET_EVENT_RULES.get(symbol.upper())
    if not rules:
        return False
    currency = str(event.get("currency") or "").upper()
    country = str(event.get("country") or "").upper()
    impact = str(event.get("impact_level") or "").lower()
    if impact not in {"high", "medium"}:
        return False
    if currency and currency in rules.get("currencies", set()):
        return True
    if country and country in rules.get("countries", set()):
        return True
    text = " ".join(
        str(event.get(key) or "") for key in ("event_name", "country", "currency")
    ).casefold()
    return any(keyword in text for keyword in rules.get("keywords", set()))


def _market_event_matches_asset(symbol: str, row: dict) -> bool:
    rules = ASSET_EVENT_RULES.get(symbol.upper())
    if not rules:
        return False
    currencies = rules.get("currencies", set())
    countries = rules.get("countries", set())
    keywords = rules.get("keywords", set())

    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    text_parts: list[str] = []
    for key in ("currency", "country"):
        value = str(payload.get(key) or "").strip().upper()
        if value in currencies or value in countries:
            return True
        text_parts.append(value)

    for entity in row.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        entity_type = str(entity.get("entity_type") or "").upper()
        canonical = str(entity.get("canonical_id") or "").strip().upper()
        if entity_type == "CURRENCY" and canonical in currencies:
            return True
        if entity_type == "COUNTRY" and canonical in countries:
            return True
        text_parts.append(canonical)
        text_parts.append(str(entity.get("display_name") or ""))

    for market in row.get("markets") or []:
        if not isinstance(market, dict):
            continue
        value = (
            str(market.get("symbol") or market.get("canonical_id") or "")
            .strip()
            .upper()
        )
        if value in currencies or value in countries:
            return True
        text_parts.append(value)

    text_parts.append(str(row.get("title") or ""))
    text_parts.append(str(row.get("event_type") or ""))
    folded = " ".join(text_parts).casefold()
    return any(keyword in folded for keyword in keywords)


# --------------------------------------------------------------------------
# Bounded SQL
# --------------------------------------------------------------------------

_MARKET_SQL = """
WITH requested(symbol) AS (
    SELECT unnest(CAST(:symbols AS TEXT[]))
)
SELECT requested.symbol,
       latest.close AS last_price,
       latest.timestamp AS last_timestamp,
       prior5.close AS prior_5m_close,
       day_first.open AS day_open,
       recent.samples AS samples,
       vol.recent_range AS vol_recent_range,
       vol.prior_range AS vol_prior_range
FROM requested
LEFT JOIN LATERAL (
    SELECT close, timestamp
    FROM market_data
    WHERE symbol = requested.symbol
      AND timeframe = 'PRICE'
      AND source IN ('oanda', 'demo')
    ORDER BY timestamp DESC
    LIMIT 1
) latest ON TRUE
LEFT JOIN LATERAL (
    SELECT close
    FROM market_data_5m
    WHERE symbol = requested.symbol
      AND bucket <= :cutoff_5m
    ORDER BY bucket DESC
    LIMIT 1
) prior5 ON TRUE
LEFT JOIN LATERAL (
    SELECT open
    FROM market_data_5m
    WHERE symbol = requested.symbol
      AND bucket >= :day_start
    ORDER BY bucket ASC
    LIMIT 1
) day_first ON TRUE
LEFT JOIN LATERAL (
    SELECT count(*) AS samples
    FROM market_data
    WHERE symbol = requested.symbol
      AND timeframe = 'PRICE'
      AND timestamp >= :samples_since
) recent ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COALESCE(SUM(CASE WHEN b.rn BETWEEN 1 AND 12 THEN b.rng END), 0) AS recent_range,
        COALESCE(SUM(CASE WHEN b.rn BETWEEN 13 AND 24 THEN b.rng END), 0) AS prior_range
    FROM (
        SELECT (high - low) AS rng,
               ROW_NUMBER() OVER (ORDER BY bucket DESC) AS rn
        FROM market_data_5m
        WHERE symbol = requested.symbol
          AND bucket <= :cutoff_5m
        ORDER BY bucket DESC
        LIMIT 24
    ) b
) vol ON TRUE
"""

_CATALYST_SQL = """
SELECT DISTINCT ON (w.instrument_symbol)
       w.instrument_symbol, w.horizon, w.reaction_state, w.target_at, w.event_at,
       e.event_type, e.payload ->> 'title' AS event_title
FROM event_reaction_windows w
LEFT JOIN market_events e ON e.id = w.event_id
WHERE w.instrument_symbol = ANY(CAST(:symbols AS TEXT[]))
  AND w.horizon IN ('5m', '15m', '30m')
  AND w.reaction_state IN ('pending', 'persistence', 'reversal', 'mixed')
ORDER BY w.instrument_symbol, w.target_at DESC, w.id DESC
"""

_OPINION_SQL = """
SELECT scope, direction, created_at, published_at
FROM structured_opinions
WHERE lifecycle_status = 'published'
ORDER BY published_at DESC NULLS LAST, created_at DESC
LIMIT :limit
"""

_DRAWER_BUCKETS_SQL = """
SELECT bucket, close
FROM market_data_5m
WHERE symbol = :symbol
ORDER BY bucket DESC
LIMIT :limit
"""

_DRAWER_SUMMARY_SQL = """
SELECT
    latest.close AS last_price,
    latest.timestamp AS last_timestamp,
    day_first.open AS day_open,
    prior_day.close AS prior_day_close,
    vol.recent_range AS vol_recent_range,
    vol.prior_range AS vol_prior_range
FROM (SELECT 1) AS one
LEFT JOIN LATERAL (
    SELECT close, timestamp
    FROM market_data
    WHERE symbol = :symbol AND timeframe = 'PRICE'
    ORDER BY timestamp DESC
    LIMIT 1
) latest ON TRUE
LEFT JOIN LATERAL (
    SELECT open
    FROM market_data_5m
    WHERE symbol = :symbol AND bucket >= :day_start
    ORDER BY bucket ASC
    LIMIT 1
) day_first ON TRUE
LEFT JOIN LATERAL (
    SELECT close
    FROM market_data_1d
    WHERE symbol = :symbol AND bucket < :day_start
    ORDER BY bucket DESC
    LIMIT 1
) prior_day ON TRUE
LEFT JOIN LATERAL (
    SELECT
        COALESCE(SUM(CASE WHEN b.rn BETWEEN 1 AND 12 THEN b.rng END), 0) AS recent_range,
        COALESCE(SUM(CASE WHEN b.rn BETWEEN 13 AND 24 THEN b.rng END), 0) AS prior_range
    FROM (
        SELECT (high - low) AS rng,
               ROW_NUMBER() OVER (ORDER BY bucket DESC) AS rn
        FROM market_data_5m
        WHERE symbol = :symbol AND bucket <= :cutoff_5m
        ORDER BY bucket DESC
        LIMIT 24
    ) b
) vol ON TRUE
"""

_RELATED_EVENTS_SQL = """
SELECT e.id::text AS id, e.event_type, e.source, e.observed_at, e.effective_at,
       e.published_at, e.payload ->> 'title' AS title,
       e.payload, e.entities, e.markets, m.score AS materiality_score
FROM market_events e
JOIN event_materiality m ON m.event_id = e.id
WHERE m.decision = 'route'
  AND (e.effective_at >= :since OR e.published_at >= :since)
ORDER BY e.effective_at DESC NULLS LAST, e.published_at DESC NULLS LAST,
         e.ingested_at DESC NULLS LAST
LIMIT :limit
"""

_ATOM_DETAIL_SQL = """
SELECT id::text AS id, invalidation_conditions, created_at
FROM analysis_atoms
WHERE id::text = ANY(CAST(:ids AS TEXT[]))
ORDER BY created_at DESC
LIMIT :limit
"""


# --------------------------------------------------------------------------
# Grid loader
# --------------------------------------------------------------------------


def load_watchlist_grid(
    config,
    *,
    view: str = "watchlist",
    sort: str = "symbol",
    direction: str = "asc",
    limit: int = 100,
) -> dict:
    """Bounded watchlist rows for the cockpit grid; fail-soft on DB errors."""
    view, sort, direction = _validate_grid_params(view, sort, direction)
    try:
        bounded_limit = max(1, min(MAX_GRID_LIMIT, int(limit)))
    except (TypeError, ValueError):
        bounded_limit = MAX_GRID_LIMIT
    try:
        return _load_watchlist_grid_rows(config, view, sort, direction, bounded_limit)
    except Exception:
        logger.warning("watchlist_grid_unavailable", view=view)
        return {
            "available": False,
            "rows": [],
            "view": view,
            "sort": sort,
            "direction": direction,
        }


def _load_watchlist_grid_rows(
    config, view: str, sort: str, direction: str, bounded_limit: int
) -> dict:
    symbols = sorted(_view_symbols(config, view))[:bounded_limit]
    if not symbols:
        return {
            "available": True,
            "rows": [],
            "view": view,
            "sort": sort,
            "direction": direction,
        }
    now = datetime.now(UTC)
    cutoff_5m = now - timedelta(minutes=5)
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    samples_since = now - timedelta(hours=24)

    market_rows = query_many(
        _MARKET_SQL,
        params={
            "symbols": symbols,
            "cutoff_5m": cutoff_5m,
            "day_start": day_start,
            "samples_since": samples_since,
        },
        config=config,
    )
    catalyst_rows = query_many(
        _CATALYST_SQL,
        params={"symbols": symbols},
        config=config,
    )
    opinions = query_many(
        _OPINION_SQL,
        params={"limit": OPINION_FETCH_LIMIT},
        config=config,
    )

    market_by_symbol = {row.get("symbol"): row for row in market_rows}
    catalyst_by_symbol = {}
    for catalyst in catalyst_rows:
        catalyst_by_symbol.setdefault(catalyst.get("instrument_symbol"), catalyst)

    rows = [
        _build_grid_row(
            config,
            symbol,
            market_by_symbol.get(symbol) or {},
            catalyst_by_symbol.get(symbol),
            opinions,
            now,
        )
        for symbol in symbols
    ]
    rows.sort(key=_sort_key(sort, direction))
    if sort == "symbol" and direction == "desc":
        rows.reverse()
    return {
        "available": True,
        "rows": rows,
        "view": view,
        "sort": sort,
        "direction": direction,
    }


def _sort_key(sort: str, direction: str):
    """Ascending sort key; None always sorts last, in both directions."""
    reverse = direction == "desc"

    def key(row: dict) -> tuple:
        if sort == "symbol":
            return (0, str(row.get("symbol") or "").upper())
        if sort == "last":
            value = row.get("last_price")
        elif sort == "chg_5m":
            value = row.get("chg_5m")
        elif sort == "chg_day":
            value = row.get("chg_day")
        else:
            value = row.get("freshness")
        if value is None:
            return (1, 0.0)
        numeric = float(value)
        return (0, -numeric if reverse else numeric)

    return key


def _build_grid_row(
    config, symbol: str, market: dict, catalyst, opinions, now: datetime
) -> dict:
    last_price = _float_or_none(market.get("last_price"))
    last_timestamp = _as_utc(market.get("last_timestamp"))
    prior_5m = _float_or_none(market.get("prior_5m_close"))
    day_open = _float_or_none(market.get("day_open"))

    chg_5m = _pct_change(last_price, prior_5m)
    chg_day = _pct_change(last_price, day_open)
    vol_state = _vol_state(
        market.get("vol_recent_range"), market.get("vol_prior_range")
    )

    label, analysis_ts = _interpretation_for(config, symbol, opinions)
    price_age = _age_minutes(last_timestamp, now) if last_timestamp else None
    analysis_age = _age_minutes(analysis_ts, now) if analysis_ts else None
    if price_age is not None and analysis_age is not None:
        freshness = price_age + analysis_age
    else:
        freshness = price_age if price_age is not None else analysis_age

    catalyst_text = None
    if catalyst:
        title = str(
            catalyst.get("event_title") or catalyst.get("event_type") or ""
        ).strip()
        horizon = str(catalyst.get("horizon") or "")
        if title:
            catalyst_text = f"{title} · {horizon}" if horizon else title

    return {
        "symbol": symbol,
        "last_price": last_price,
        "last_price_text": f"{last_price:.5g}" if last_price is not None else None,
        "chg_5m": chg_5m,
        "chg_5m_text": _pct_text(chg_5m),
        "chg_day": chg_day,
        "chg_day_text": _pct_text(chg_day),
        "samples": market.get("samples"),
        "vol_state": vol_state,
        "catalyst_text": catalyst_text,
        "interpretation": label,
        "interpretation_class": _interpretation_class(label),
        "price_age_minutes": price_age,
        "analysis_age_minutes": analysis_age,
        "freshness": freshness,
        "price_age_text": _age_text(price_age),
        "analysis_age_text": _age_text(analysis_age),
        "price_iso": _iso(last_timestamp),
        "analysis_iso": _iso(analysis_ts),
    }


def _interpretation_for(
    config, symbol: str, opinions: list[dict]
) -> tuple[str, object]:
    """Latest published opinion scoped to the symbol, else the newest atom.

    Returns (label, analysis_timestamp) where the timestamp feeds the
    analysis freshness cell. Never returns an action word.
    """
    folded = symbol.casefold()
    for opinion in opinions:
        scope = str(opinion.get("scope") or "")
        if folded in scope.casefold():
            label = _direction_label(opinion.get("direction"))
            timestamp = opinion.get("created_at") or opinion.get("published_at")
            return label, timestamp
    try:
        context = load_atom_context(config, subject_id=symbol, limit=1)
    except Exception:
        context = {}
    atoms = context.get("atoms") if isinstance(context, dict) else None
    if atoms:
        atom = atoms[0]
        timestamp = atom.get("created_at") or atom.get("published_at")
        return _atom_interpretation(atom), timestamp
    return "neutral", None


# --------------------------------------------------------------------------
# Asset drawer loader
# --------------------------------------------------------------------------


def load_asset_drawer(config, symbol, *, request=None) -> dict:
    """Bounded drawer payload for one known symbol; ValueError on unknown."""
    normalized = _validate_symbol(symbol)
    if normalized not in _known_symbols(config):
        raise ValueError("unknown symbol")

    now = datetime.now(UTC)
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    cutoff_5m = now - timedelta(minutes=5)

    bucket_rows = query_many(
        _DRAWER_BUCKETS_SQL,
        params={"symbol": normalized, "limit": DRAWER_BUCKETS},
        config=config,
    )
    bucket_rows.reverse()
    chart_labels = [_iso(row.get("bucket")) for row in bucket_rows]
    chart_values = [_float_or_none(row.get("close")) for row in bucket_rows]

    summary = (
        query_one(
            _DRAWER_SUMMARY_SQL,
            params={
                "symbol": normalized,
                "day_start": day_start,
                "cutoff_5m": cutoff_5m,
            },
            config=config,
        )
        or {}
    )
    last_price = _float_or_none(summary.get("last_price"))
    day_open = _float_or_none(summary.get("day_open"))
    prior_day_close = _float_or_none(summary.get("prior_day_close"))
    session_change = _pct_change(last_price, day_open)
    day_change = _pct_change(last_price, prior_day_close)
    vol_state = _vol_state(
        summary.get("vol_recent_range"), summary.get("vol_prior_range")
    )

    return {
        "symbol": normalized,
        "available": True,
        "last_price": last_price,
        "last_price_text": f"{last_price:.5g}" if last_price is not None else None,
        "last_iso": _iso(summary.get("last_timestamp")),
        "session_change": session_change,
        "session_change_text": _pct_text(session_change),
        "day_change": day_change,
        "day_change_text": _pct_text(day_change),
        "vol_state": vol_state,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
        "chart_point_count": len(chart_values),
        "related_events": _related_events(config, normalized, now),
        "macro_exposures": _macro_exposures(config, normalized),
        "catalysts": _matched_catalysts(config, normalized, request),
        "atoms": _drawer_atoms(config, normalized),
        "notes": [],  # operator notes/hypotheses mutation routes are a later slice
    }


def _related_events(config, symbol: str, now: datetime) -> list[dict]:
    try:
        rows = query_many(
            _RELATED_EVENTS_SQL,
            params={
                "since": now - timedelta(days=EVENT_LOOKBACK_DAYS),
                "limit": RELATED_EVENTS_FETCH_LIMIT,
            },
            config=config,
        )
    except Exception:
        return []
    matched = [row for row in rows if _market_event_matches_asset(symbol, row)]
    events = []
    for row in matched[:6]:
        effective_iso = _iso(row.get("effective_at")) or _iso(row.get("published_at"))
        score = _float_or_none(row.get("materiality_score"))
        events.append(
            {
                "title": str(
                    row.get("title") or row.get("event_type") or "market event"
                ).strip()[:300],
                "event_type": str(row.get("event_type") or ""),
                "source": str(row.get("source") or ""),
                "effective_at": effective_iso,
                "effective_text": (
                    effective_iso[:16].replace("T", " ") if effective_iso else None
                ),
                "score": round(score, 3) if score is not None else None,
            }
        )
    return events


def _matched_catalysts(config, symbol: str, request) -> list[dict]:
    request = request or _bare_request()
    try:
        data = get_events_upcoming_data(request=request, days=14)
    except Exception:
        return []
    events = data.get("events") if isinstance(data, dict) else None
    if not events:
        return []
    matched = [event for event in events if _econ_event_matches_asset(symbol, event)]
    return matched[:6]


def _macro_exposures(config, symbol: str) -> list[dict]:
    rules = ASSET_EVENT_RULES.get(symbol.upper()) or {}
    categories: set[str] = set()
    for currency in rules.get("currencies", set()):
        categories |= _MACRO_CATEGORIES_BY_CURRENCY.get(currency, set())
    for keyword in rules.get("keywords", set()):
        category = _KEYWORD_CATEGORIES.get(keyword)
        if category:
            categories.add(category)
    if not categories:
        return []
    try:
        dashboard = get_macro_dashboard()
    except Exception:
        return []
    indicators = dashboard.get("indicators") if isinstance(dashboard, dict) else None
    if not indicators:
        return []
    return [
        indicator
        for indicator in indicators
        if (indicator.get("category") or "") in categories
    ][:6]


def _drawer_atoms(config, symbol: str) -> list[dict]:
    try:
        context = load_atom_context(config, subject_id=symbol, limit=DRAWER_ATOMS_LIMIT)
    except Exception:
        return []
    atoms = context.get("atoms") if isinstance(context, dict) else None
    if not atoms:
        return []
    ids = [atom.get("id") for atom in atoms if atom.get("id")]
    details: dict = {}
    if ids:
        try:
            rows = query_many(
                _ATOM_DETAIL_SQL,
                params={"ids": ids, "limit": len(ids)},
                config=config,
            )
        except Exception:
            rows = []
        details = {row.get("id"): row for row in rows}
    for atom in atoms:
        extra = details.get(atom.get("id")) or {}
        atom["invalidation_conditions"] = _bounded_strings(
            extra.get("invalidation_conditions")
        )
        atom["created_at"] = _iso(extra.get("created_at"))
    return atoms


def _bare_request() -> StarletteRequest:
    """Minimal request for timezone-aware helpers when none is in scope."""
    return StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/partials/dashboard/asset",
            "headers": [],
            "query_string": b"",
            "server": ("localhost", 80),
            "scheme": "http",
        }
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _live_updates_enabled(config: dict) -> bool:
    return config.get("event_pipeline", {}).get("sse", {}).get("enabled") is True


def _templates(request: Request):
    return request.app.state.templates


@router.get("/partials/dashboard/watchlist-grid")
def partial_watchlist_grid(
    request: Request,
    view: str = "watchlist",
    sort: str = "symbol",
    direction: str = "asc",
):
    try:
        view, sort, direction = _validate_grid_params(view, sort, direction)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    config = app_config.load_config()
    try:
        data = load_watchlist_grid(config, view=view, sort=sort, direction=direction)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _templates(request).TemplateResponse(
        request,
        "partials/watchlist_grid.html",
        {
            "request": request,
            "grid": data,
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )


@router.get("/partials/dashboard/asset/{symbol}")
def partial_asset_drawer(request: Request, symbol: str):
    config = app_config.load_config()
    try:
        drawer = load_asset_drawer(config, symbol, request=request)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown symbol") from None
    except Exception:
        logger.warning("asset_drawer_unavailable", symbol=symbol)
        raise HTTPException(status_code=503, detail="Asset data unavailable") from None
    return _templates(request).TemplateResponse(
        request,
        "partials/asset_drawer.html",
        {
            "request": request,
            "drawer": drawer,
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )
