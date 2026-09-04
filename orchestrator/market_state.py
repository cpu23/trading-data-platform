"""Bounded, deterministic market-state calculations.

Functions accept caller-owned sessions and never commit, roll back, or close
them. Missing observations are represented by ``{"value": None, "reason": ...}``.

Market-state runtime settings are validated through the shared
``contracts.runtime_config.MarketStateConfig`` model: unknown or misspelled
fields are rejected, and documented fields with no consumer are flagged.
``baskets`` and ``yield_curves`` settings are *definitions* (which symbols form
a basket, which observation keys form a spread label) and are never treated as
numeric observations. History reads are bounded by time and a per-symbol row
limit (window semantics), null/non-finite OHLC rows are filtered, and snapshots
carry market-state-v2 provenance that is persisted alongside the features.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import text

MARKET_STATE_VERSION = "market-state-v2"
_MAX_ROWS = 10_000
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,64}$")
_LOOKBACK_SCALES: dict[str, timedelta] = {
    "minutes": timedelta(minutes=1),
    "hours": timedelta(hours=1),
    "days": timedelta(days=1),
}

_logger = logging.getLogger(__name__)

# Every documented market-state runtime field and the code path that consumes
# it. A field accepted by the validated model but absent here is flagged by
# validate_market_state_config as having no consumer (a dead knob).
MARKET_STATE_CONSUMERS: dict[str, str] = {
    "enabled": "price-tick routing gate (events/routing.py)",
    "rows_per_symbol": "per-symbol price-history row limit (_fetch_rows)",
    "snapshot_limit": "default result limit (list_market_features)",
    "trend_bars": "trend slope bar window (_feature_for_rows)",
    "zscore_bars": "intraday zscore trailing bar window (_feature_for_rows)",
    "volatility_bars": "realized-volatility close slice (_feature_for_rows)",
    "lookback": "price-history lookback window (_fetch_rows)",
    "state_thresholds": "state-change and classification deadbands (compute_feature_snapshot)",
    "state_thresholds.trend_slope_epsilon": "trend slope deadband (_feature_for_rows)",
    "state_thresholds.high_volatility_threshold": "volatility level classification (compute_feature_snapshot, always-present)",
    "state_thresholds.high_correlation_threshold": "cross-asset correlation classification (compute_feature_snapshot)",
    "baskets": "basket definitions; member symbols are fetched and breadth is computed from observations",
    "yield_curves": "yield-curve spread definitions; observation keys fetched from macro_series (payload yields override)",
}


def _market_state_model() -> Any:
    """Return the shared validated runtime model for the market-state section."""
    try:
        from contracts.runtime_config import MarketStateConfig
    except ImportError as exc:  # pragma: no cover - hard integration dependency
        raise RuntimeError(
            "contracts.runtime_config.MarketStateConfig is required to validate "
            "market-state runtime configuration"
        ) from exc
    return MarketStateConfig


def _lookback_timedelta(lookback: Any) -> timedelta:
    """Convert an explicit-unit ``{value, unit}`` lookback to a timedelta."""
    value = getattr(lookback, "value", None)
    unit = getattr(lookback, "unit", None)
    scale = _LOOKBACK_SCALES.get(unit)
    if scale is None or not isinstance(value, int) or value < 1:
        raise ValueError(f"invalid market_state.lookback: {lookback!r}")
    return value * scale


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


def intraday_zscore(
    values: Iterable[Any], current: Any | None = None, *, window: int | None = None
) -> float | None:
    clean = [item for item in (_finite(value) for value in values) if item is not None]
    if window is not None:
        clean = clean[-max(1, int(window)) :]
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


_MIN_CORRELATION_PAIRS = 5
_CORRELATION_METHOD = "close_to_close_returns_on_shared_timestamps"


def _close_returns(
    rows: Iterable[Mapping[str, Any]],
) -> dict[datetime, float]:
    """Close-to-close simple returns keyed by the return observation time.

    A return at timestamp ``t`` compares the close at ``t`` with the previous
    close, so pairing two symbols on the shared return timestamps aligns their
    observation cadence instead of raw list positions.
    """
    epoch = datetime.min.replace(tzinfo=UTC)
    returns: dict[datetime, float] = {}
    previous: float | None = None
    for row in sorted(rows, key=lambda item: _utc(item.get("timestamp")) or epoch):
        timestamp = _utc(row.get("timestamp"))
        close = _finite(row.get("close"))
        if timestamp is None or close is None:
            continue
        if previous is not None and previous != 0:
            value = close / previous - 1.0
            if math.isfinite(value):
                returns[timestamp] = value
        previous = close
    return returns


def returns_correlation(
    left_rows: Sequence[Mapping[str, Any]],
    right_rows: Sequence[Mapping[str, Any]],
    *,
    min_pairs: int = _MIN_CORRELATION_PAIRS,
) -> tuple[float | None, int, str | None]:
    """Pearson correlation of close-to-close returns aligned on shared
    observation timestamps.

    Returns ``(value, pairs, reason)``: ``value`` is null when fewer than
    ``min_pairs`` shared return timestamps exist (``insufficient_paired_samples``)
    or the aligned returns have zero variance (``constant_returns``).
    """
    left = _close_returns(left_rows)
    right = _close_returns(right_rows)
    shared = sorted(left.keys() & right.keys())
    if len(shared) < max(2, int(min_pairs)):
        return None, len(shared), "insufficient_paired_samples"
    first = [left[timestamp] for timestamp in shared]
    second = [right[timestamp] for timestamp in shared]
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
        return None, len(shared), "constant_returns"
    value = numerator / denominator
    if not math.isfinite(value):
        return None, len(shared), "non_finite"
    return value, len(shared), None


def basket_breadth(
    changes: Mapping[str, Any], *, members: Iterable[str] | None = None
) -> dict[str, Any]:
    """Breadth over finite changes, optionally restricted to basket members.

    ``members`` is the basket *definition* (which symbols count); ``changes``
    are numeric observations. Symbols outside the definition never contribute.
    """
    if members is not None:
        member_set = set(members)
        changes = {name: value for name, value in changes.items() if name in member_set}
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


def yield_curve_spreads(
    curve: Mapping[str, Any], *, definitions: Mapping[str, Sequence[str]] | None = None
) -> dict[str, dict[str, Any]]:
    """Spread between long and short yields for each configured label.

    ``definitions`` maps a spread label to the two observation keys (for
    example ``{"us_10y_2y": ["DGS10", "DGS2"]}``); ``curve`` holds the numeric
    observations keyed by those names. Without definitions the legacy fixed
    tenor aliases (3m/2y/5y/10y/30y) are used.
    """
    if definitions is not None:
        result = {}
        for label, keys in definitions.items():
            if not isinstance(keys, (list, tuple)) or len(keys) != 2:
                result[label] = _metric(reason="invalid_definition")
                continue
            long_value = _finite(curve.get(keys[0]))
            short_value = _finite(curve.get(keys[1]))
            result[label] = _metric(
                long_value - short_value
                if long_value is not None and short_value is not None
                else None,
                None
                if long_value is not None and short_value is not None
                else "missing_data",
            )
        return result
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


def volatility_level(
    value: Any,
    *,
    threshold: float = 0.0,
    name: str = "volatility",
    reason: str | None = None,
) -> dict[str, Any]:
    """Classify the current realized-volatility level against a threshold."""
    value_finite = _finite(value)
    if value_finite is None:
        return _metric(f"{name}_unknown", reason or "missing_data")
    label = f"{name}_high" if abs(value_finite) >= abs(threshold) else f"{name}_normal"
    return _metric(label, volatility=value_finite)


def correlation_state_change(
    current: Any, previous: Any, *, threshold: float = 0.0
) -> dict[str, Any]:
    return state_change(current, previous, threshold=threshold, name="correlation")


def correlation_level(
    value: Any,
    *,
    threshold: float = 0.75,
    name: str = "correlation",
    reason: str | None = None,
    pairs: int | None = None,
) -> dict[str, Any]:
    """Classify the absolute correlation level against a threshold."""
    value_finite = _finite(value)
    if value_finite is None:
        metric = _metric(f"{name}_unknown", reason or "missing_data")
    else:
        label = (
            f"{name}_high" if abs(value_finite) >= abs(threshold) else f"{name}_normal"
        )
        metric = _metric(label, correlation=value_finite)
    if pairs is not None:
        metric["pairs"] = pairs
    return metric


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


def _usable_ohlc(row: Mapping[str, Any]) -> bool:
    """A row is usable only when open/high/low/close are all finite."""
    return all(
        _finite(row.get(field)) is not None
        for field in ("open", "high", "low", "close")
    )


def _normalise_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalise rows, drop rows with null/non-finite OHLC, sort by timestamp."""
    normalised = []
    for raw in rows:
        row = _row_mapping(raw)
        timestamp = _utc(row.get("timestamp", row.get("bucket")))
        if timestamp is None or not _usable_ohlc(row):
            continue
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
    rows_per_symbol: int,
) -> list[dict[str, Any]]:
    """Fetch up to ``rows_per_symbol`` rows per symbol within the lookback.

    A window function partitions by symbol so one dense symbol cannot starve
    the others, and null/non-finite OHLC rows are excluded before ranking so
    they do not consume per-symbol slots. ``NaN`` compares greater than
    ``'Infinity'`` in PostgreSQL, so the finite predicates also exclude it.
    """
    statement = text("""
SELECT symbol, timestamp, open, high, low, close, volume, source
FROM (
    SELECT symbol, timestamp, open, high, low, close, volume, source,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS _rank
    FROM market_data
    WHERE symbol = ANY(:symbols) AND timeframe = 'PRICE'
      AND timestamp >= :start_at AND timestamp <= :as_of
      AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL
      AND open < 'Infinity'::double precision AND open > '-Infinity'::double precision
      AND high < 'Infinity'::double precision AND high > '-Infinity'::double precision
      AND low < 'Infinity'::double precision AND low > '-Infinity'::double precision
      AND close < 'Infinity'::double precision AND close > '-Infinity'::double precision
) ranked
WHERE _rank <= :rows_per_symbol
ORDER BY symbol, timestamp DESC""")
    result = session.execute(
        statement,
        {
            "symbols": list(symbols),
            "start_at": as_of - lookback,
            "as_of": as_of,
            "rows_per_symbol": rows_per_symbol,
        },
    )
    return _normalise_rows(_result_rows(result))


_YIELD_ROWS_PER_KEY = 1


def _yield_keys(definitions: Mapping[str, Sequence[str]]) -> list[str]:
    """Distinct configured yield observation keys across all spread labels."""
    keys: list[str] = []
    for members in definitions.values():
        for key in members or ():
            candidate = _symbol(key)
            if candidate and candidate not in keys:
                keys.append(candidate)
    return keys


def _fetch_yield_observations(
    session: Any,
    series_ids: Sequence[str],
    as_of: datetime,
    *,
    rows_per_key: int = _YIELD_ROWS_PER_KEY,
) -> dict[str, float]:
    """Latest finite value at/before ``as_of`` per macro-series key.

    FRED series (e.g. DGS10, DGS2) are stored in ``macro_series``, not
    ``market_data``; a window function partitions by series_id so each key
    contributes at most ``rows_per_key`` rows, and null/non-finite values are
    excluded before ranking.
    """
    if not series_ids:
        return {}
    statement = text("""
SELECT series_id, observed_at, value
FROM (
    SELECT series_id, observed_at, value,
           ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY observed_at DESC) AS _rank
    FROM macro_series
    WHERE series_id = ANY(:series_ids)
      AND observed_at <= :as_of
      AND value IS NOT NULL
      AND value < 'Infinity'::double precision AND value > '-Infinity'::double precision
) ranked
WHERE _rank <= :rows_per_key
ORDER BY series_id, observed_at DESC""")
    result = session.execute(
        statement,
        {
            "series_ids": list(series_ids),
            "as_of": as_of,
            "rows_per_key": rows_per_key,
        },
    )
    observations: dict[str, float] = {}
    for row in _result_rows(result):
        series_id = _symbol(row.get("series_id"))
        value = _finite(row.get("value"))
        if series_id is not None and value is not None:
            observations[series_id] = value
    return observations


def _feature_for_rows(
    rows: Sequence[Mapping[str, Any]],
    as_of: datetime,
    *,
    trend_bars: int = 20,
    zscore_bars: int | None = None,
    volatility_bars: int | None = None,
    trend_slope_epsilon: float = 0.0,
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
    if volatility_bars is not None:
        volatility_closes = [
            row.get("close") for row in usable[-max(1, int(volatility_bars)) :]
        ]
    else:
        volatility_closes = [row.get("close") for row in intraday]
    realized = realized_volatility(volatility_closes)
    zscore = intraday_zscore(
        [row.get("close") for row in intraday], last, window=zscore_bars
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
            realized, None if realized is not None else "insufficient_history"
        ),
        "intraday_zscore": _metric(
            zscore, None if zscore is not None else "insufficient_or_constant_data"
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
    window = max(2, min(int(trend_bars), len(usable)))
    values = [_finite(row.get("close")) for row in usable[-window:]]
    if len(values) >= 2 and all(value is not None for value in values):
        x_mean, y_mean = (len(values) - 1) / 2, mean(values)
        slope = sum(
            (index - x_mean) * (value - y_mean) for index, value in enumerate(values)
        ) / sum((index - x_mean) ** 2 for index in range(len(values)))
        result["trend"] = _metric(
            "up"
            if slope > trend_slope_epsilon
            else "down"
            if slope < -trend_slope_epsilon
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


def _symbol_changes(
    rows_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    as_of: datetime,
) -> dict[str, float]:
    """Last-minus-previous close per symbol; the basket breadth observations."""
    changes: dict[str, float] = {}
    for name, rows in rows_by_symbol.items():
        usable = [row for row in rows if row["timestamp"] <= as_of]
        if len(usable) >= 2:
            current = _finite(usable[-1].get("close"))
            previous = _finite(usable[-2].get("close"))
            if current is not None and previous is not None:
                changes[name] = current - previous
    return changes


def _unavailable(features: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Collect explicit missing-data reasons, recursing into nested metrics."""
    result: dict[str, str] = {}
    for key, value in features.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            if value.get("value") is None and value.get("reason"):
                result[name] = value["reason"]
            else:
                result.update(_unavailable(value, name))
    return result


def _provenance(
    source_event_id: Any,
    lookback: timedelta,
    rows_per_symbol: int,
    symbols_requested: int,
    config_issues: Sequence[str] | None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "version": MARKET_STATE_VERSION,
        "source_event_id": str(source_event_id)
        if source_event_id is not None
        else None,
        "source_table": "market_data",
        "lookback_seconds": int(lookback.total_seconds()),
        "rows_per_symbol": rows_per_symbol,
        "ohlc_filtered": True,
        "symbols_requested": symbols_requested,
        "correlation_method": _CORRELATION_METHOD,
        "correlation_min_pairs": _MIN_CORRELATION_PAIRS,
        "yield_source": "macro_series",
        "yield_rows_per_key": _YIELD_ROWS_PER_KEY,
    }
    if config_issues:
        provenance["config_flags"] = list(config_issues)
    return provenance


def compute_feature_snapshot(
    session: Any,
    symbol: str,
    as_of: Any = None,
    source_event_id: Any = None,
    *,
    symbols: Sequence[str] | None = None,
    market_rows: Sequence[Mapping[str, Any]] | None = None,
    lookback: timedelta = timedelta(days=7),
    rows_per_symbol: int = 5000,
    trend_bars: int = 20,
    zscore_bars: int | None = None,
    volatility_bars: int | None = 30,
    trend_slope_epsilon: float = 0.0,
    yield_curves: Mapping[str, Sequence[str]] | None = None,
    yield_observations: Mapping[str, Any] | None = None,
    baskets: Mapping[str, Sequence[str]] | None = None,
    high_volatility_threshold: float = 0.0,
    high_correlation_threshold: float = 0.75,
    config_issues: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compute one snapshot; SQL source reads are bounded by time and a
    per-symbol row limit.

    ``baskets`` and ``yield_curves`` are *definitions*: they say which symbols
    form a basket and which observation keys form a spread label. Numeric
    observations flow through ``yield_observations`` (or are fetched from
    ``macro_series`` — latest finite value at/before ``as_of`` per key — when
    not supplied) and the fetched price rows; definitions are never treated as
    observations. Realized volatility uses exactly the last ``volatility_bars``
    closes and is classified against ``high_volatility_threshold``
    (always-present ``volatility_level``); cross-asset correlations are
    computed from close-to-close returns aligned on shared observation
    timestamps (minimum ``_MIN_CORRELATION_PAIRS`` paired returns) and
    classified against ``high_correlation_threshold``.
    """
    clean_symbol = _symbol(symbol)
    parsed_as_of = _utc(as_of) or datetime.now(UTC)
    if clean_symbol is None:
        return {
            "symbol": str(symbol),
            "as_of": parsed_as_of.isoformat(),
            "features": {"last": _metric(reason="invalid_symbol")},
            "unavailable": {"symbol": "invalid_symbol"},
            "provenance": _provenance(source_event_id, lookback, 0, 0, config_issues),
        }
    clean_symbols = [clean_symbol]
    for candidate in symbols or ():
        candidate_symbol = _symbol(candidate)
        if candidate_symbol and candidate_symbol not in clean_symbols:
            clean_symbols.append(candidate_symbol)
    bounded_limit = max(1, min(int(rows_per_symbol), _MAX_ROWS))
    if market_rows is not None:
        rows = _normalise_rows(market_rows)
    else:
        rows = _fetch_rows(
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
        trend_bars=trend_bars,
        zscore_bars=zscore_bars,
        volatility_bars=volatility_bars,
        trend_slope_epsilon=trend_slope_epsilon,
    )
    if yield_curves is not None:
        observations = yield_observations
        if not isinstance(observations, Mapping):
            observations = _fetch_yield_observations(
                session, _yield_keys(yield_curves), parsed_as_of
            )
        features["yield_curve_spreads"] = yield_curve_spreads(
            observations or {},
            definitions=yield_curves,
        )
    if baskets is not None:
        observations = _symbol_changes(by_symbol, parsed_as_of)
        features["basket_breadth"] = {
            name: basket_breadth(observations, members=members)
            for name, members in baskets.items()
        }
    series = {
        name: [row for row in rows if row["timestamp"] <= parsed_as_of]
        for name, rows in by_symbol.items()
        if rows
    }
    if len(series) >= 2:
        features["correlations"] = {}
        for left, right in combinations(sorted(series), 2):
            value, pairs, reason = returns_correlation(
                series[left], series[right], min_pairs=_MIN_CORRELATION_PAIRS
            )
            features["correlations"][f"{left}:{right}"] = correlation_level(
                value,
                threshold=high_correlation_threshold,
                reason=reason,
                pairs=pairs,
            )
    realized = features.get("realized_volatility", {})
    features["volatility_level"] = volatility_level(
        realized.get("value"),
        threshold=high_volatility_threshold,
        reason=realized.get("reason"),
    )
    unavailable = _unavailable(features)
    return {
        "symbol": clean_symbol,
        "as_of": parsed_as_of.isoformat(),
        "source_event_id": str(source_event_id)
        if source_event_id is not None
        else None,
        "features": _json_safe(features),
        "unavailable": _json_safe(unavailable),
        "provenance": _provenance(
            source_event_id, lookback, bounded_limit, len(clean_symbols), config_issues
        ),
    }


def save_feature_snapshot(session: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Upsert without taking ownership of the caller transaction.

    Provenance (market-state-v2) is persisted inside the features JSONB under
    the reserved ``provenance`` key so downstream readers can see how and with
    what bounds the snapshot was produced.
    """
    symbol, as_of = _symbol(snapshot.get("symbol")), _utc(snapshot.get("as_of"))
    source_event_id = str(snapshot.get("source_event_id") or "").strip()
    if symbol is None or as_of is None or not source_event_id:
        raise ValueError("symbol, as_of, and source_event_id are required")
    features = _json_safe(snapshot.get("features") or {})
    unavailable = _json_safe(snapshot.get("unavailable") or {})
    provenance = (
        snapshot.get("provenance")
        if isinstance(snapshot.get("provenance"), Mapping)
        else {}
    )
    persisted_provenance = {
        "version": MARKET_STATE_VERSION,
        **_json_safe(provenance),
    }
    persisted_features = {**features, "provenance": persisted_provenance}
    json.dumps(persisted_features, allow_nan=False)
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
            "features": json.dumps(persisted_features, allow_nan=False),
            "unavailable": json.dumps(unavailable, allow_nan=False),
        },
    )
    return {
        **dict(snapshot),
        "symbol": symbol,
        "as_of": as_of.isoformat(),
        "features": persisted_features,
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


def validate_market_state_config(
    settings: Any, *, consumers: Mapping[str, str] | None = None
) -> tuple[Any, list[str]]:
    """Validate the market-state configuration section.

    Returns ``(validated, issues)`` where ``validated`` is the immutable
    runtime snapshot and ``issues`` list documented fields with no consumer.
    Unknown or misspelled fields raise ``ValueError`` (they are rejected) so a
    misconfigured profile fails fast instead of silently doing nothing.
    """
    if settings is None:
        settings = {}
    if not isinstance(settings, Mapping):
        raise ValueError("market_state configuration must be a mapping")
    model = _market_state_model()
    try:
        validated = model.model_validate(settings)
    except ValueError as exc:
        raise ValueError(f"invalid market_state configuration: {exc}") from exc
    consumers_map = MARKET_STATE_CONSUMERS if consumers is None else consumers
    issues = [
        f"market_state.{name} is documented but has no consumer"
        for name in type(validated).model_fields
        if name not in consumers_map
    ]
    thresholds = getattr(validated, "state_thresholds", None)
    if thresholds is not None and hasattr(type(thresholds), "model_fields"):
        issues.extend(
            f"market_state.state_thresholds.{name} is documented but has no consumer"
            for name in type(thresholds).model_fields
            if f"state_thresholds.{name}" not in consumers_map
        )
    return validated, issues


def update_price_features(
    session: Any,
    event: Any,
    config: Mapping[str, Any] | None = None,
    *,
    now: Any = None,
) -> dict[str, Any]:
    """Compute and persist features for a normalized ``price_tick`` event.

    The ``market_state`` config section is validated through the shared runtime
    model; unknown or misspelled fields raise ``ValueError``. Basket and
    yield-curve settings are definitions: basket member symbols are fetched
    from the price history, and yield observations come from ``macro_series``
    (latest finite value per configured key) unless the event payload
    explicitly overrides them via ``payload["yields"]``.
    """
    event_values = _event_mapping(event)
    event_payload = event_values.get("payload")
    payload = event_payload if isinstance(event_payload, Mapping) else event_values
    settings, issues = validate_market_state_config(
        config.get("market_state") if isinstance(config, Mapping) else None
    )
    for issue in issues:
        _logger.warning("market-state config: %s", issue)
    symbol, timestamp = (
        payload.get("symbol"),
        payload.get("timestamp") or now or datetime.now(UTC),
    )
    source_event_id = (
        event_values.get("id")
        or event_values.get("event_id")
        or event_values.get("source_event_id")
    )
    lookback = _lookback_timedelta(settings.lookback)
    # Only basket members join the price-history fetch; yield-curve keys are
    # observation names resolved from the event payload (``yields``), not
    # market_data symbols, so they never belong in the fetch.
    extra_symbols: list[str] = []
    for members in (settings.baskets or {}).values():
        extra_symbols.extend(members)
    yield_payload = payload.get("yields")
    snapshot = compute_feature_snapshot(
        session,
        symbol,
        timestamp,
        source_event_id,
        symbols=extra_symbols,
        lookback=lookback,
        rows_per_symbol=settings.rows_per_symbol,
        trend_bars=settings.trend_bars,
        zscore_bars=settings.zscore_bars,
        volatility_bars=settings.volatility_bars,
        trend_slope_epsilon=settings.state_thresholds.trend_slope_epsilon,
        yield_curves=settings.yield_curves or None,
        yield_observations=(
            yield_payload if isinstance(yield_payload, Mapping) else None
        ),
        baskets=settings.baskets or None,
        high_volatility_threshold=settings.state_thresholds.high_volatility_threshold,
        high_correlation_threshold=settings.state_thresholds.high_correlation_threshold,
        config_issues=issues or None,
    )
    if source_event_id:
        return save_feature_snapshot(session, snapshot)
    snapshot["unavailable"]["source_event_id"] = "missing_data"
    return snapshot


def list_market_features(
    session: Any,
    symbols: Sequence[str] | None = None,
    *,
    limit: int | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List persisted feature snapshots; ``limit`` defaults to
    ``market_state.snapshot_limit`` when a config is supplied."""
    clean_symbols = [
        candidate for symbol in symbols or () if (candidate := _symbol(symbol))
    ]
    if limit is None:
        settings, _ = validate_market_state_config(
            config.get("market_state") if isinstance(config, Mapping) else None
        )
        limit = settings.snapshot_limit
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
