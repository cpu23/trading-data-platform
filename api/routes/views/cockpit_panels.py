"""Cockpit panels compatibility data layer: cross-asset, catalysts, delta.

Every loader in this module is model-free: data comes from bounded SQL or
from the existing ``routes.json`` readers (``get_briefing_latest``).  Each
sub-fetch is isolated in its own ``try/except`` so one unavailable source degrades only its own field/panel
and never the whole partial.  No SQL text or exception detail is ever
surfaced to the client.

The dashboard compact top strip lives in ``routes.views.dashboard_strip`` and
the change feed lives in ``routes.views.news``; this module keeps the compat
partial surfaces (``/partials/dashboard/cross-asset``,
``/partials/dashboard/catalysts``, ``/partials/dashboard/briefing-delta``)
and the loaders behind them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request

import config as app_config
from db import query_many
from routes.json.briefing import get_briefing_latest
from routes.views.asset_rules import ASSET_EVENT_RULES
from routes.views.dashboard_strip import (
    as_datetime,
    countdown_display,
    iso,
    primary_zone,
    time_display,
)

router = APIRouter()

DIRECTION_THRESHOLD_PCT = 0.05

CATALYST_DAYS = 7
CATALYST_LIMIT = 6
COUNTRY_TO_CURRENCY = {
    "US": "USD",
    "EU": "EUR",
    "GB": "GBP",
    "JP": "JPY",
    "AU": "AUD",
    "CN": "CNY",
}

_LATEST_MACRO_SQL = """
    SELECT DISTINCT ON (series_id) series_id, value, observed_at
    FROM macro_series
    WHERE series_id IN (SELECT jsonb_array_elements_text(:series_ids::jsonb))
    ORDER BY series_id, observed_at DESC
    LIMIT :limit
"""

_CATALYSTS_SQL = """
    SELECT event_id, event_name, country, scheduled_at, impact_level,
           consensus, previous, source, metadata
    FROM econ_events
    WHERE scheduled_at >= :start AND scheduled_at <= :end
      AND lower(COALESCE(impact_level, '')) IN ('high', 'medium', 'low')
      AND (
          country IN ('US', 'EU', 'GB', 'JP', 'AU', 'CN')
          OR metadata ->> 'currency' IN ('USD', 'EUR', 'GBP', 'JPY', 'AUD', 'CNY')
      )
    ORDER BY scheduled_at ASC
    LIMIT 100
"""

_VOL_TERM_SQL = """
    SELECT tf, symbol, high, low, close FROM (
        SELECT '1m' AS tf, symbol, high, low, close FROM market_data_1m
        UNION ALL SELECT '5m', symbol, high, low, close FROM market_data_5m
        UNION ALL SELECT '15m', symbol, high, low, close FROM market_data_15m
        UNION ALL SELECT '1h', symbol, high, low, close FROM market_data_1h
        UNION ALL SELECT '1d', symbol, high, low, close FROM market_data_1d
    ) buckets
    WHERE symbol IN (SELECT jsonb_array_elements_text(:symbols::jsonb))
    ORDER BY tf, symbol, bucket DESC
    LIMIT 250
"""


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def json_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def pct_display(value) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return f"{value:+.2f}%"


def direction(change, threshold: float = DIRECTION_THRESHOLD_PCT) -> str | None:
    if not isinstance(change, (int, float)):
        return None
    if change > threshold:
        return "up"
    if change < -threshold:
        return "down"
    return "flat"


def _pair_change(pair) -> float | None:
    """Percent change between the last two daily buckets (newest first)."""
    if len(pair) < 2:
        return None
    prev_close, last_close = pair[1][1], pair[0][1]
    if prev_close is None or last_close is None or not prev_close:
        return None
    return (last_close / prev_close - 1.0) * 100.0


def _last_two_buckets(config: dict, symbols: list[str]) -> dict:
    """Latest two distinct 1d buckets per symbol: {SYMBOL: [(bucket, close)]}."""
    symbols = [str(symbol).strip().upper() for symbol in symbols if symbol]
    if not symbols:
        return {}
    rows = query_many(
        """
        SELECT symbol, bucket, close FROM market_data_1d
        WHERE symbol IN (SELECT jsonb_array_elements_text(:symbols::jsonb))
        ORDER BY bucket DESC
        LIMIT :limit
        """,
        params={
            "symbols": json.dumps(symbols),
            "limit": min(3 * len(symbols), 120),
        },
        config=config,
    )
    result: dict[str, list] = {}
    seen: dict[str, set] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        bucket = row.get("bucket")
        if symbol not in result:
            result[symbol] = []
            seen[symbol] = set()
        if bucket in seen[symbol] or len(result[symbol]) >= 2:
            continue
        seen[symbol].add(bucket)
        result[symbol].append((bucket, row.get("close")))
    return result


def _watchlist_symbols(config: dict) -> list[str]:
    symbols: list[str] = []
    for group in config.get("watchlist", {}).values():
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict):
                symbol = item.get("symbol")
            elif isinstance(item, str):
                symbol = item
            else:
                continue
            symbol = str(symbol or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _indices(config: dict) -> list[str]:
    indices: list[str] = []
    for group in config.get("watchlist", {}).values():
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict) and item.get("type") == "index":
                symbol = str(item.get("symbol") or "").strip().upper()
                if symbol and symbol not in indices:
                    indices.append(symbol)
    return indices or ["SP500"]


def _metadata_value(metadata) -> dict:
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    denominator = math.sqrt(
        sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys)
    )
    return numerator / denominator if denominator else None


def _normalize_section(value) -> str:
    if isinstance(value, list):
        parts = [str(part) for part in value]
    else:
        parts = [str(value or "")]
    return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()


def _section_fingerprint(value) -> str:
    return hashlib.sha256(_normalize_section(value).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Cross-asset panels
# ---------------------------------------------------------------------------


def _panel_yield_curve(config: dict) -> dict:
    panel: dict = {
        "key": "yield_curve",
        "title": "Yield curve",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        indicators = [
            item
            for item in config.get("dashboard", {}).get("indicators", [])
            if item.get("category") == "yield_curve"
        ]
        if not indicators:
            return panel
        series_ids = [
            str(item.get("series_id")) for item in indicators if item.get("series_id")
        ]
        rows = query_many(
            _LATEST_MACRO_SQL,
            params={"series_ids": json.dumps(series_ids), "limit": len(series_ids)},
            config=config,
        )
        latest = {str(row.get("series_id")): row for row in rows}
        points = []
        for indicator in indicators:
            row = latest.get(str(indicator.get("series_id")))
            value = row.get("value") if row else None
            if not isinstance(value, (int, float)):
                continue  # absent data must never fabricate a point
            precision = int(indicator.get("precision") or 2)
            points.append(
                {
                    "label": indicator.get("label") or indicator.get("series_id"),
                    "value": value,
                    "display": f"{value:.{precision}f}",
                    "direction": None,
                    "detail": indicator.get("series_id"),
                }
            )
        if not points:
            return panel
        values = [
            point["value"]
            for point in points
            if isinstance(point["value"], (int, float))
        ]
        if len(values) >= 2:
            spread = max(values) - min(values)
            points.append(
                {
                    "label": "Spread",
                    "value": spread,
                    "display": f"{spread:.2f}",
                    "direction": None,
                    "detail": "max − min",
                }
            )
        panel["rows"] = points
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_dollar_real_yields(config: dict) -> dict:
    panel: dict = {
        "key": "dollar_real_yields",
        "title": "Dollar vs real yields",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        rows_list = []
        pair = _last_two_buckets(config, ["DXY"]).get("DXY") or []
        change = _pair_change(pair)
        last_close = pair[0][1] if pair else None
        if last_close is not None:
            rows_list.append(
                {
                    "label": "DXY",
                    "value": last_close,
                    "display": f"{last_close:.2f}",
                    "direction": direction(change),
                    "detail": pct_display(change),
                }
            )
        indicators = [
            item
            for item in config.get("dashboard", {}).get("indicators", [])
            if item.get("category") == "real_yields"
        ]
        if indicators:
            series_ids = [
                str(item.get("series_id"))
                for item in indicators
                if item.get("series_id")
            ]
            rows = query_many(
                _LATEST_MACRO_SQL,
                params={
                    "series_ids": json.dumps(series_ids),
                    "limit": len(series_ids),
                },
                config=config,
            )
            latest = {str(row.get("series_id")): row for row in rows}
            for indicator in indicators:
                row = latest.get(str(indicator.get("series_id")))
                value = row.get("value") if row else None
                if not isinstance(value, (int, float)):
                    continue
                rows_list.append(
                    {
                        "label": indicator.get("label") or indicator.get("series_id"),
                        "value": value,
                        "display": f"{value:.2f}",
                        "direction": None,
                        "detail": indicator.get("series_id"),
                    }
                )
        if not rows_list:
            return panel
        panel["rows"] = rows_list
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_equity_breadth(config: dict) -> dict:
    panel: dict = {
        "key": "equity_breadth",
        "title": "Equity breadth",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        indices = _indices(config)
        buckets = _last_two_buckets(config, indices)
        rows_list = []
        for symbol in indices:
            change = _pair_change(buckets.get(symbol) or [])
            if change is None:
                continue
            rows_list.append(
                {
                    "label": symbol,
                    "value": change,
                    "display": pct_display(change),
                    "direction": direction(change),
                    "detail": None,
                }
            )
        if not rows_list:
            return panel
        advancing = sum(1 for row in rows_list if row["direction"] == "up")
        declining = sum(1 for row in rows_list if row["direction"] == "down")
        flat = sum(1 for row in rows_list if row["direction"] == "flat")
        panel["rows"] = rows_list
        panel["summary"] = (
            f"{advancing} advancing · {declining} declining · {flat} flat"
        )
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_volatility_term_structure(config: dict) -> dict:
    panel: dict = {
        "key": "volatility_term_structure",
        "title": "Volatility term structure",
        "rows": [],
        "available": False,
        "summary": None,
    }
    symbols = [
        str(symbol).strip().upper()
        for symbol in (config.get("dashboard", {}).get("volatility_symbols") or [])
        if symbol
    ]
    if not symbols:
        # No configured vol symbols: skip cleanly without touching the DB.
        return panel
    try:
        rows = query_many(
            _VOL_TERM_SQL,
            params={"symbols": json.dumps(symbols)},
            config=config,
        )
        latest: dict[tuple, tuple] = {}
        for row in rows:
            timeframe = row.get("tf")
            symbol = str(row.get("symbol") or "").strip().upper()
            key = (timeframe, symbol)
            if key in latest:
                continue
            high, low, close = row.get("high"), row.get("low"), row.get("close")
            if high is None or low is None or not close:
                continue
            latest[key] = (high, low, close)
        by_tf: dict[str, list[float]] = {}
        for (timeframe, _symbol), (high, low, close) in latest.items():
            by_tf.setdefault(timeframe, []).append((high - low) / close * 100.0)
        points = []
        for timeframe in sorted(by_tf):
            average = sum(by_tf[timeframe]) / len(by_tf[timeframe])
            points.append(
                {
                    "label": timeframe,
                    "value": average,
                    "display": f"{average:.3f}%",
                    "direction": None,
                    "detail": f"{len(by_tf[timeframe])} symbols",
                }
            )
        if not points:
            return panel
        panel["rows"] = points
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_commodity_impulse(config: dict) -> dict:
    panel: dict = {
        "key": "commodity_impulse",
        "title": "Commodity impulse",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        buckets = _last_two_buckets(config, ["WTICOUSD", "XAUUSD"])
        rows_list = []
        for symbol in ("WTICOUSD", "XAUUSD"):
            change = _pair_change(buckets.get(symbol) or [])
            if change is None:
                continue
            rows_list.append(
                {
                    "label": symbol,
                    "value": change,
                    "display": pct_display(change),
                    "direction": direction(change),
                    "detail": None,
                }
            )
        if not rows_list:
            return panel
        panel["rows"] = rows_list
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_rolling_correlation(config: dict) -> dict:
    panel: dict = {
        "key": "rolling_correlation",
        "title": "EURUSD vs SP500 rolling correlation",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        rows = query_many(
            """
            SELECT symbol, bucket, close FROM market_data_1d
            WHERE symbol IN (SELECT jsonb_array_elements_text(:symbols::jsonb))
            ORDER BY symbol, bucket DESC
            LIMIT 80
            """,
            params={"symbols": json.dumps(["EURUSD", "SP500"])},
            config=config,
        )
        by_symbol: dict[str, dict] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            by_symbol.setdefault(symbol, {})[row.get("bucket")] = row.get("close")
        eur = by_symbol.get("EURUSD", {})
        spx = by_symbol.get("SP500", {})
        aligned = sorted(set(eur) & set(spx))
        if len(aligned) < 20:
            return panel  # hide when the window is too short to be meaningful

        def returns_of(bucket_list: list) -> list[tuple[float, float]]:
            pairs = [
                (eur[bucket], spx[bucket])
                for bucket in bucket_list
                if bucket in eur and bucket in spx
            ]
            pairs = [
                (x, y)
                for x, y in pairs
                if x is not None and y is not None and x != 0 and y != 0
            ]
            return [
                (x2 / x1 - 1.0, y2 / y1 - 1.0)
                for (x1, y1), (x2, y2) in zip(pairs, pairs[1:], strict=False)
            ]

        def corr_of(bucket_list: list) -> float | None:
            returns = returns_of(bucket_list)
            if len(returns) < 2:
                return None
            return _pearson([x for x, _ in returns], [y for _, y in returns])

        current = corr_of(aligned[-20:])
        prior = corr_of(aligned[-40:-20]) if len(aligned) >= 40 else None
        rows_list = []
        if current is not None:
            rows_list.append(
                {
                    "label": "Current 20d",
                    "value": current,
                    "display": f"{current:.2f}",
                    "direction": None,
                    "detail": None,
                }
            )
        if prior is not None:
            rows_list.append(
                {
                    "label": "Prior 20d",
                    "value": prior,
                    "display": f"{prior:.2f}",
                    "direction": None,
                    "detail": None,
                }
            )
            if current is not None:
                delta = current - prior
                rows_list.append(
                    {
                        "label": "Change",
                        "value": delta,
                        "display": f"{delta:+.2f}",
                        "direction": direction(delta, threshold=0.0),
                        "detail": None,
                    }
                )
        if not rows_list:
            return panel
        panel["rows"] = rows_list
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_session_heatmap(config: dict) -> dict:
    panel: dict = {
        "key": "session_heatmap",
        "title": "Session heat map",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        symbols = _watchlist_symbols(config)
        if not symbols:
            return panel
        buckets = _last_two_buckets(config, symbols)
        rows_list = []
        for symbol in symbols:
            change = _pair_change(buckets.get(symbol) or [])
            if change is None:
                continue
            rows_list.append(
                {
                    "label": symbol,
                    "value": change,
                    "display": pct_display(change),
                    "direction": direction(change),
                    "detail": None,
                }
            )
        if not rows_list:
            return panel
        panel["rows"] = rows_list
        panel["available"] = True
    except Exception:
        pass
    return panel


def _panel_change_since_event(config: dict) -> dict:
    panel: dict = {
        "key": "change_since_event",
        "title": "Change since last major event",
        "rows": [],
        "available": False,
        "summary": None,
    }
    try:
        rows = query_many(
            """
            SELECT DISTINCT ON (symbol) symbol, as_of, features
            FROM market_feature_snapshots
            ORDER BY symbol, as_of DESC
            LIMIT :limit
            """,
            params={"limit": 50},
            config=config,
        )
        rows_list = []
        for row in rows:
            features = json_obj(row.get("features"))
            percentage_move = features.get("percentage_move")
            if percentage_move is None:
                continue
            if isinstance(percentage_move, dict):
                items = []
                for horizon, value in percentage_move.items():
                    if isinstance(value, dict):
                        value = value.get("value")
                    items.append((str(horizon), value))
                items.sort()
            else:
                items = [("move", percentage_move)]
            for horizon, value in items:
                if not isinstance(value, (int, float)):
                    continue
                rows_list.append(
                    {
                        "label": f"{row.get('symbol')} {horizon}",
                        "value": value,
                        "display": pct_display(value),
                        "direction": direction(value),
                        "detail": time_display(row.get("as_of")),
                    }
                )
        if not rows_list:
            return panel
        panel["rows"] = rows_list
        panel["available"] = True
    except Exception:
        pass
    return panel


def load_cross_asset(config: dict) -> dict:
    """All cross-asset panels; absent data yields ``available: False``."""
    panels = [
        _panel_yield_curve(config),
        _panel_dollar_real_yields(config),
        _panel_equity_breadth(config),
        _panel_volatility_term_structure(config),
        _panel_commodity_impulse(config),
        _panel_rolling_correlation(config),
        _panel_session_heatmap(config),
        _panel_change_since_event(config),
    ]
    return {
        "panels": panels,
        "available": any(panel["available"] for panel in panels),
    }


# ---------------------------------------------------------------------------
# Catalysts
# ---------------------------------------------------------------------------


def _impacted_symbols(
    event: dict, symbols: list[str], currency, limit: int = 4
) -> list[str]:
    """Watchlist symbols exposed to an event via ASSET_EVENT_RULES."""
    text = " ".join(
        str(event.get(key) or "") for key in ("event_name", "country", "source")
    ).lower()
    country = str(event.get("country") or "").upper()
    matched: list[str] = []
    for symbol in symbols:
        rules = ASSET_EVENT_RULES.get(symbol)
        if not rules:
            continue
        normalized_currency = str(currency or "").upper()
        if normalized_currency and normalized_currency in rules.get(
            "currencies", set()
        ):
            matched.append(symbol)
        elif country and country in rules.get("countries", set()):
            matched.append(symbol)
        elif any(keyword in text for keyword in rules.get("keywords", set())):
            matched.append(symbol)
        if len(matched) >= limit:
            break
    return matched


def load_catalysts(config: dict) -> dict:
    """Up to six high-impact econ events spread across the next 7 days.

    Mirrors the filtering used by ``get_events_upcoming_data(days=7)`` with a
    bounded SQL read (no request object required) and spreads the pick
    round-robin across days so one busy day cannot crowd out the rest.
    """
    try:
        now = datetime.now(UTC)
        rows = query_many(
            _CATALYSTS_SQL,
            params={"start": now, "end": now + timedelta(days=CATALYST_DAYS)},
            config=config,
        )
    except Exception:
        return {"available": False, "catalysts": [], "days": CATALYST_DAYS}

    zone = primary_zone(config)
    today_key = datetime.now(zone).date().isoformat()

    def day_label(day_key: str) -> str:
        if day_key == today_key:
            return "Today"
        try:
            return datetime.strptime(day_key, "%Y-%m-%d").strftime("%A")
        except ValueError:
            return day_key

    watchlist_symbols = _watchlist_symbols(config)
    normalized = []
    for row in rows:
        if str(row.get("impact_level") or "").lower() != "high":
            continue
        scheduled = as_datetime(row.get("scheduled_at"))
        day_key = scheduled.astimezone(zone).date().isoformat() if scheduled else ""
        minutes = None
        if scheduled is not None:
            minutes = max(0, int((scheduled - datetime.now(UTC)).total_seconds() // 60))
        metadata = _metadata_value(row.get("metadata"))
        currency = (
            metadata.get("currency")
            or row.get("currency")
            or COUNTRY_TO_CURRENCY.get(row.get("country"), row.get("country"))
        )
        normalized.append(
            {
                "event_name": row.get("event_name"),
                "country": row.get("country"),
                "currency": currency,
                "scheduled_at": iso(scheduled),
                "day_key": day_key,
                "day_label": day_label(day_key),
                "countdown_minutes": minutes,
                "countdown_display": countdown_display(minutes),
                "impact_level": row.get("impact_level"),
                "impacted_symbols": _impacted_symbols(row, watchlist_symbols, currency),
            }
        )
    by_day: dict[str, list[dict]] = {}
    for event in normalized:
        by_day.setdefault(event["day_key"], []).append(event)
    picked: list[dict] = []
    queues = list(by_day.values())
    while queues and len(picked) < CATALYST_LIMIT:
        next_queues = []
        for queue in queues:
            picked.append(queue.pop(0))
            if queue:
                next_queues.append(queue)
        queues = next_queues
    picked.sort(key=lambda event: event.get("scheduled_at") or "")
    return {
        "available": True,
        "catalysts": picked[:CATALYST_LIMIT],
        "days": CATALYST_DAYS,
    }


# ---------------------------------------------------------------------------
# Briefing delta
# ---------------------------------------------------------------------------


def _delta_bullets(latest_sections: dict, previous_sections: dict | None) -> list[str]:
    latest_fingerprints = {
        label: _section_fingerprint(value) for label, value in latest_sections.items()
    }
    if not previous_sections:
        return [f"Section: {label}" for label in sorted(latest_fingerprints)]
    previous_fingerprints = {
        label: _section_fingerprint(value) for label, value in previous_sections.items()
    }
    bullets = []
    for label in sorted(latest_fingerprints):
        if label not in previous_fingerprints:
            bullets.append(f"New section: {label}")
        elif previous_fingerprints[label] != latest_fingerprints[label]:
            bullets.append(f"Changed section: {label}")
        else:
            bullets.append(f"Unchanged section: {label}")
    for label in sorted(set(previous_fingerprints) - set(latest_fingerprints)):
        bullets.append(f"Removed section: {label}")
    return bullets


_MISSING = object()


def load_briefing_delta(config: dict, latest=_MISSING) -> dict:
    """Latest briefing vs the previous briefing record; model-free delta.

    ``latest`` may be preloaded by the caller (dashboard page, merged briefing
    partial) so the latest-briefing read happens exactly once per request;
    the default fetches it here for standalone callers and the compat partial.
    """
    if latest is _MISSING:
        try:
            latest = get_briefing_latest()
        except Exception:
            return {"available": False, "bullets": [], "atoms": [], "latest_date": None}
    if not isinstance(latest, dict):
        return {"available": False, "bullets": [], "atoms": [], "latest_date": None}
    latest_sections = latest.get("sections")
    if not isinstance(latest_sections, dict):
        latest_sections = {}

    previous_sections: dict | None = None
    try:
        rows = query_many(
            """
            SELECT opinion_id, scope, summary, created_at, reasoning
            FROM structured_opinions
            WHERE opinion_type = 'briefing' AND lifecycle_status = 'published'
            ORDER BY created_at DESC
            LIMIT 2
            """,
            config=config,
        )
        if len(rows) >= 2:
            previous_sections = {"interpretation": rows[-1].get("summary")}
    except Exception:
        previous_sections = None

    bullets = _delta_bullets(latest_sections, previous_sections)
    atoms: list[dict] = []
    try:
        atom_rows = query_many(
            """
            SELECT claim_type, COUNT(*) AS count
            FROM analysis_atoms
            WHERE status IN ('validated', 'published')
            GROUP BY claim_type
            ORDER BY claim_type
            LIMIT 20
            """,
            config=config,
        )
        atoms = [
            {"claim_type": row.get("claim_type"), "count": int(row.get("count") or 0)}
            for row in atom_rows
        ]
    except Exception:
        pass
    return {
        "available": True,
        "latest_date": latest.get("briefing_date") or iso(latest.get("created_at")),
        "bullets": bullets,
        "atoms": atoms,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _live_updates_enabled(config: dict) -> bool:
    return config.get("event_pipeline", {}).get("sse", {}).get("enabled") is True


@router.get("/partials/dashboard/cross-asset")
def partial_cross_asset(request: Request):
    config = app_config.load_config()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/cross_asset.html",
        {
            "request": request,
            "cross_asset": load_cross_asset(config),
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )


@router.get("/partials/dashboard/catalysts")
def partial_catalysts(request: Request):
    config = app_config.load_config()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/catalysts.html",
        {
            "request": request,
            "catalysts": load_catalysts(config),
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )


@router.get("/partials/dashboard/briefing-delta")
def partial_briefing_delta(request: Request):
    config = app_config.load_config()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "partials/briefing_delta.html",
        {
            "request": request,
            "briefing_delta": load_briefing_delta(config),
            "live_updates_enabled": _live_updates_enabled(config),
        },
    )
