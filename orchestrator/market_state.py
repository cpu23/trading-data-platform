"""Bounded, deterministic market-state calculations.

Functions accept caller-owned sessions and never commit, roll back, or close
them. Missing observations are represented by ``{"value": None, "reason": ...}``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import text

_MAX_ROWS = 10_000
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,64}$")


def _metric(
    value: Any = None, reason: str | None = None, **extra: Any
) -> dict[str, Any]:
    result = {"value": value, "reason": reason}
    result.update(extra)
    return result


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _symbol(value: Any) -> str | None:
    candidate = str(value or "").strip()
    return candidate if _SYMBOL_RE.fullmatch(candidate) else None


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def calculate_return(current: Any, previous: Any) -> float | None:
    """Calculate a simple return, rejecting missing and zero denominators."""
    current_value, previous_value = _finite(current), _finite(previous)
    if current_value is None or previous_value is None or previous_value == 0:
        return None
    result = current_value / previous_value - 1.0
    return result if math.isfinite(result) else None


def return_result(current: Any, previous: Any) -> dict[str, Any]:
    current_value, previous_value = _finite(current), _finite(previous)
    if current_value is None or previous_value is None:
        return _metric(reason="missing_data")
    if previous_value == 0:
        return _metric(reason="zero_denominator")
    result = calculate_return(current_value, previous_value)
    return _metric(result, None if result is not None else "non_finite")


def realized_volatility(closes: Iterable[Any]) -> float | None:
    """Close-to-close realized volatility: sqrt(sum(log-return squared))."""
    values = [
        value for value in (_finite(item) for item in closes) if value is not None
    ]
    if len(values) < 2 or any(value <= 0 for value in values):
        return None
    result = math.sqrt(
        sum(
            math.log(right / left) ** 2
            for left, right in zip(values, values[1:], strict=False)
        )
    )
    return result if math.isfinite(result) else None


def intraday_zscore(values: Iterable[Any], current: Any | None = None) -> float | None:
    clean = [item for item in (_finite(value) for value in values) if item is not None]
    current_value = _finite(
        current if current is not None else (clean[-1] if clean else None)
    )
    if len(clean) < 2 or current_value is None:
        return None
    deviation = pstdev(clean)
    if deviation == 0:
        return None
    result = (current_value - mean(clean)) / deviation
    return result if math.isfinite(result) else None


def session_high_low_position(last: Any, high: Any, low: Any) -> float | None:
    last_value, high_value, low_value = _finite(last), _finite(high), _finite(low)
    if (
        last_value is None
        or high_value is None
        or low_value is None
        or high_value == low_value
    ):
        return None
    result = (last_value - low_value) / (high_value - low_value)
    return result if math.isfinite(result) else None


def _pair_values(
    left: Sequence[Any], right: Sequence[Any]
) -> tuple[list[float], list[float]]:
    if (
        left
        and isinstance(left[0], Mapping)
        and right
        and isinstance(right[0], Mapping)
    ):
        right_by_time = {_utc(row.get("timestamp")): row for row in right}
        pairs = []
        for row in left:
            other = right_by_time.get(_utc(row.get("timestamp")))
            first = _finite(row.get("close"))
            second = _finite(other.get("close")) if other else None
            if first is not None and second is not None:
                pairs.append((first, second))
        return [pair[0] for pair in pairs], [pair[1] for pair in pairs]
    pairs = [(_finite(a), _finite(b)) for a, b in zip(left, right, strict=False)]
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def correlation(left: Sequence[Any], right: Sequence[Any]) -> float | None:
    """Pearson correlation on aligned finite values; undefined is null."""
    first, second = _pair_values(left, right)
    if len(first) < 2:
        return None
    first_mean, second_mean = mean(first), mean(second)
    numerator = sum(
        (a - first_mean) * (b - second_mean)
        for a, b in zip(first, second, strict=False)
    )
    denominator = math.sqrt(
        sum((a - first_mean) ** 2 for a in first)
        * sum((b - second_mean) ** 2 for b in second)
    )
    if denominator == 0:
        return None
    result = numerator / denominator
    return result if math.isfinite(result) else None


def cross_asset_correlations(
    series: Mapping[str, Sequence[Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for left, right in combinations(sorted(series), 2):
        value = correlation(series[left], series[right])
        result[f"{left}:{right}"] = _metric(
            value, "insufficient_or_constant_data" if value is None else None
        )
    return result


def basket_breadth(changes: Mapping[str, Any]) -> dict[str, Any]:
    clean = {
        name: value
        for name, value in ((name, _finite(value)) for name, value in changes.items())
        if value is not None
    }
    if not clean:
        return _metric(
            reason="missing_data", advancing=0, declining=0, unchanged=0, total=0
        )
    advancing = sum(value > 0 for value in clean.values())
    declining = sum(value < 0 for value in clean.values())
    unchanged = len(clean) - advancing - declining
    return _metric(
        advancing / len(clean),
        advancing=advancing,
        declining=declining,
        unchanged=unchanged,
        total=len(clean),
    )


def yield_curve_spreads(curve: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    aliases = {
        "3m": ("3m", "3mo", "DGS3MO", "DTB3"),
        "2y": ("2y", "2yr", "DGS2"),
        "5y": ("5y", "5yr", "DGS5"),
        "10y": ("10y", "10yr", "DGS10"),
        "30y": ("30y", "30yr", "DGS30"),
    }
    values = {
        tenor: next((_finite(curve[name]) for name in names if name in curve), None)
        for tenor, names in aliases.items()
    }
    result = {}
    for label, long_name, short_name in (
        ("10y_2y", "10y", "2y"),
        ("10y_3m", "10y", "3m"),
        ("5y_2y", "5y", "2y"),
        ("30y_10y", "30y", "10y"),
    ):
        long_value, short_value = values[long_name], values[short_name]
        result[label] = _metric(
            long_value - short_value
            if long_value is not None and short_value is not None
            else None,
            None
            if long_value is not None and short_value is not None
            else "missing_data",
        )
    return result


def state_change(
    current: Any, previous: Any, *, threshold: float = 0.0, name: str = "state"
) -> dict[str, Any]:
    current_value, previous_value = _finite(current), _finite(previous)
    if current_value is None or previous_value is None:
        return _metric(f"{name}_unknown", "missing_data")
    difference = current_value - previous_value
    label = (
        f"{name}_rising"
        if difference > abs(threshold)
        else f"{name}_falling"
        if difference < -abs(threshold)
        else f"{name}_stable"
    )
    return _metric(label, change=difference)


def volatility_state_change(
    current: Any, previous: Any, *, threshold: float = 0.0
) -> dict[str, Any]:
    return state_change(current, previous, threshold=threshold, name="volatility")


def correlation_state_change(
    current: Any, previous: Any, *, threshold: float = 0.0
) -> dict[str, Any]:
    return state_change(current, previous, threshold=threshold, name="correlation")


def _row_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    try:
        return dict(row._mapping)
    except AttributeError:
        fields = (
            "symbol",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source",
        )
        return {name: value for name, value in zip(fields, row, strict=False)}


def _normalise_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    normalised = []
    for raw in rows:
        row = _row_mapping(raw)
        timestamp = _utc(row.get("timestamp", row.get("bucket")))
        if timestamp is not None:
            row["timestamp"] = timestamp
            normalised.append(row)
    return sorted(normalised, key=lambda row: row["timestamp"])


def _result_rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [_row_mapping(row) for row in result.mappings().all()]
    except (AttributeError, TypeError):
        try:
            return [_row_mapping(row) for row in result.fetchall()]
        except AttributeError:
            return [_row_mapping(row) for row in result]


def _fetch_rows(
    session: Any,
    symbols: Sequence[str],
    as_of: datetime,
    lookback: timedelta,
    limit: int,
) -> list[dict[str, Any]]:
    statement = text("""SELECT symbol, timestamp, open, high, low, close, volume, source
FROM market_data
WHERE symbol = ANY(:symbols) AND timeframe = 'PRICE'
  AND timestamp >= :start_at AND timestamp <= :as_of
ORDER BY timestamp DESC
LIMIT :row_limit""")
    result = session.execute(
        statement,
        {
            "symbols": list(symbols),
            "start_at": as_of - lookback,
            "as_of": as_of,
            "row_limit": limit,
        },
    )
    return _normalise_rows(_result_rows(result))


def _feature_for_rows(
    rows: Sequence[Mapping[str, Any]],
    as_of: datetime,
    *,
    trend_window: int = 20,
    trend_threshold: float = 0.0,
) -> dict[str, Any]:
    ordered = _normalise_rows(rows)
    usable = [
        row
        for row in ordered
        if _finite(row.get("close")) is not None and row["timestamp"] <= as_of
    ]
    if not usable:
        return {
            "last": _metric(reason="no_rows"),
            "source_query": _metric(reason="no_rows"),
        }
    last = _finite(usable[-1].get("close"))
    day = as_of.date()
    intraday = [row for row in usable if row["timestamp"].date() == day]
    prior = [row for row in usable if row["timestamp"].date() < day]
    previous_close = _finite(prior[-1].get("close")) if prior else None
    session_open = next(
        (_finite(row.get("open")) or _finite(row.get("close")) for row in intraday),
        None,
    )
    session_high = max((_finite(row.get("high")) for row in intraday), default=None)
    session_low = min((_finite(row.get("low")) for row in intraday), default=None)
    change_value = (
        None if last is None or previous_close is None else last - previous_close
    )
    result: dict[str, Any] = {
        "last": _metric(last),
        "change": _metric(
            change_value,
            None if change_value is not None else "missing_data",
            kind="absolute",
        ),
        "previous_close": _metric(
            previous_close, None if previous_close is not None else "missing_data"
        ),
        "session_open": _metric(
            session_open, None if session_open is not None else "missing_data"
        ),
        "returns": {},
        "realized_volatility": _metric(
            realized_volatility([row.get("close") for row in intraday]),
            "insufficient_history",
        ),
        "intraday_zscore": _metric(
            intraday_zscore([row.get("close") for row in intraday], last),
            "insufficient_or_constant_data",
        ),
        "session_high_low_position": _metric(
            session_high_low_position(last, session_high, session_low),
            "missing_data"
            if session_high is None or session_low is None
            else ("zero_denominator" if session_high == session_low else None),
        ),
    }
    for label, duration in (
        ("1m", timedelta(minutes=1)),
        ("5m", timedelta(minutes=5)),
        ("30m", timedelta(minutes=30)),
        ("daily", timedelta(days=1)),
    ):
        baseline = next(
            (
                _finite(row.get("close"))
                for row in reversed(usable)
                if row["timestamp"] <= as_of - duration
            ),
            None,
        )
        result["returns"][label] = return_result(last, baseline)
    window = max(2, min(int(trend_window), len(usable)))
    values = [_finite(row.get("close")) for row in usable[-window:]]
    if len(values) >= 2 and all(value is not None for value in values):
        x_mean, y_mean = (len(values) - 1) / 2, mean(values)
        slope = sum(
            (index - x_mean) * (value - y_mean) for index, value in enumerate(values)
        ) / sum((index - x_mean) ** 2 for index in range(len(values)))
        result["trend"] = _metric(
            "up"
            if slope > trend_threshold
            else "down"
            if slope < -trend_threshold
            else "flat",
            slope=slope,
            window=window,
        )
    else:
        result["trend"] = _metric(reason="insufficient_history", window=window)
    prior_high = max((_finite(row.get("high")) for row in prior), default=None)
    prior_low = min((_finite(row.get("low")) for row in prior), default=None)
    if not intraday or prior_high is None or prior_low is None:
        result["session_break"] = _metric("unknown", "missing_prior_session")
    elif session_high is not None and session_high > prior_high:
        result["session_break"] = _metric("breakout_up")
    elif session_low is not None and session_low < prior_low:
        result["session_break"] = _metric("breakout_down")
    else:
        result["session_break"] = _metric("inside")
    return result


def compute_feature_snapshot(
    session: Any,
    symbol: str,
    as_of: Any = None,
    source_event_id: Any = None,
    *,
    symbols: Sequence[str] | None = None,
    market_rows: Sequence[Mapping[str, Any]] | None = None,
    lookback: timedelta = timedelta(days=7),
    row_limit: int = 5000,
    trend_window: int = 20,
    trend_threshold: float = 0.0,
    yields: Mapping[str, Any] | None = None,
    basket: Mapping[str, Any] | None = None,
    previous_volatility: Any = None,
) -> dict[str, Any]:
    """Compute one snapshot; SQL source reads are bounded by time and LIMIT."""
    clean_symbol = _symbol(symbol)
    parsed_as_of = _utc(as_of) or datetime.now(UTC)
    if clean_symbol is None:
        return {
            "symbol": str(symbol),
            "as_of": parsed_as_of.isoformat(),
            "features": {"last": _metric(reason="invalid_symbol")},
            "unavailable": {"symbol": "invalid_symbol"},
        }
    clean_symbols = [clean_symbol]
    for candidate in symbols or ():
        candidate_symbol = _symbol(candidate)
        if candidate_symbol and candidate_symbol not in clean_symbols:
            clean_symbols.append(candidate_symbol)
    bounded_limit = max(1, min(int(row_limit), _MAX_ROWS))
    rows = _normalise_rows(market_rows or []) or _fetch_rows(
        session, clean_symbols, parsed_as_of, lookback, bounded_limit
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {name: [] for name in clean_symbols}
    for row in rows:
        row_symbol = _symbol(row.get("symbol"))
        if row_symbol in by_symbol:
            by_symbol[row_symbol].append(row)
    features = _feature_for_rows(
        by_symbol[clean_symbol],
        parsed_as_of,
        trend_window=trend_window,
        trend_threshold=trend_threshold,
    )
    if yields is not None:
        features["yield_curve_spreads"] = yield_curve_spreads(yields)
    if basket is not None:
        features["basket_breadth"] = basket_breadth(basket)
    if previous_volatility is not None:
        features["volatility_state"] = volatility_state_change(
            features.get("realized_volatility", {}).get("value"), previous_volatility
        )
    unavailable = {
        key: value["reason"]
        for key, value in features.items()
        if isinstance(value, Mapping)
        and value.get("value") is None
        and value.get("reason")
    }
    return {
        "symbol": clean_symbol,
        "as_of": parsed_as_of.isoformat(),
        "source_event_id": str(source_event_id)
        if source_event_id is not None
        else None,
        "features": _json_safe(features),
        "unavailable": _json_safe(unavailable),
        "provenance": {
            "source_event_id": str(source_event_id)
            if source_event_id is not None
            else None,
            "source_table": "market_data",
            "bounded_row_limit": bounded_limit,
        },
    }


def save_feature_snapshot(session: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Upsert without taking ownership of the caller transaction."""
    symbol, as_of = _symbol(snapshot.get("symbol")), _utc(snapshot.get("as_of"))
    source_event_id = str(snapshot.get("source_event_id") or "").strip()
    if symbol is None or as_of is None or not source_event_id:
        raise ValueError("symbol, as_of, and source_event_id are required")
    features, unavailable = (
        _json_safe(snapshot.get("features") or {}),
        _json_safe(snapshot.get("unavailable") or {}),
    )
    json.dumps(features, allow_nan=False)
    json.dumps(unavailable, allow_nan=False)
    statement = text("""INSERT INTO market_feature_snapshots (symbol, as_of, source_event_id, features, unavailable)
VALUES (:symbol, :as_of, :source_event_id, CAST(:features AS JSONB), CAST(:unavailable AS JSONB))
ON CONFLICT (symbol, as_of, source_event_id) DO UPDATE
SET features = EXCLUDED.features, unavailable = EXCLUDED.unavailable, updated_at = NOW()""")
    session.execute(
        statement,
        {
            "symbol": symbol,
            "as_of": as_of,
            "source_event_id": source_event_id,
            "features": json.dumps(features, allow_nan=False),
            "unavailable": json.dumps(unavailable, allow_nan=False),
        },
    )
    return {
        **dict(snapshot),
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "features": features,
        "unavailable": unavailable,
    }


def _event_mapping(event: Any) -> Mapping[str, Any]:
    if isinstance(event, Mapping):
        return event
    model_dump = getattr(event, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    return {
        key: getattr(event, key)
        for key in ("id", "event_id", "source_event_id", "payload")
        if hasattr(event, key)
    }


def update_price_features(
    session: Any,
    event: Any,
    config: Mapping[str, Any] | None = None,
    *,
    now: Any = None,
) -> dict[str, Any]:
    """Compute and persist features for a normalized ``price_tick`` event."""
    event_values = _event_mapping(event)
    event_payload = event_values.get("payload")
    payload = event_payload if isinstance(event_payload, Mapping) else event_values
    settings = config.get("market_state", {}) if isinstance(config, Mapping) else {}
    symbol, timestamp = (
        payload.get("symbol"),
        payload.get("timestamp") or now or datetime.now(UTC),
    )
    source_event_id = (
        event_values.get("id")
        or event_values.get("event_id")
        or event_values.get("source_event_id")
    )
    state_thresholds = (
        settings.get("state_thresholds", {})
        if isinstance(settings.get("state_thresholds", {}), Mapping)
        else {}
    )
    snapshot = compute_feature_snapshot(
        session,
        symbol,
        timestamp,
        source_event_id,
        market_rows=None,
        lookback=timedelta(
            days=max(
                1, int(settings.get("realized_volatility_window", 1440)) // 1440 or 7
            )
        ),
        row_limit=int(settings.get("query_limit", 5000)),
        trend_window=int(settings.get("trend_window", 20)),
        trend_threshold=float(state_thresholds.get("trend", 0.0)),
        yields=settings.get("yield_curves"),
        basket=settings.get("baskets"),
    )
    if source_event_id:
        return save_feature_snapshot(session, snapshot)
    snapshot["unavailable"]["source_event_id"] = "missing_data"
    return snapshot


def list_market_features(
    session: Any, symbols: Sequence[str] | None = None, *, limit: int = 100
) -> list[dict[str, Any]]:
    clean_symbols = [
        candidate for symbol in symbols or () if (candidate := _symbol(symbol))
    ]
    bounded_limit = max(1, min(int(limit), 500))
    statement = text("""SELECT symbol, as_of, source_event_id, features, unavailable, created_at, updated_at
FROM market_feature_snapshots
WHERE (:has_symbols = FALSE OR symbol = ANY(:symbols))
ORDER BY as_of DESC
LIMIT :row_limit""")
    result = session.execute(
        statement,
        {
            "has_symbols": bool(clean_symbols),
            "symbols": clean_symbols,
            "row_limit": bounded_limit,
        },
    )
    return _json_safe(_result_rows(result))


compute_market_features = compute_feature_snapshot
calculate_yield_curve_spreads = yield_curve_spreads
calculate_cross_asset_correlations = cross_asset_correlations
calculate_basket_breadth = basket_breadth
