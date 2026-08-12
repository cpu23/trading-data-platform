"""Deterministic market-event reaction-window persistence.

Functions use a caller-owned SQLAlchemy session and never commit. Mapping,
calculation, and database lookup are deliberately bounded and deterministic.

Baseline (pre-event) and target (post-event) prices are selected with direct
directional SQL: the most recent row strictly before the event for the
baseline and the first row at or after the target for the target, each
bounded by calendar-aware trading-time windows. Path samples for
reaction-state classification and matched-horizon volatility use separate
bounded range queries. Session boundaries come from the venue calendar
(venue_calendar.py): 24x5 FX sessions, exchange sessions, observed holidays,
early closes, and DST are all reflected in baseline lookback, target
tolerance, and end-of-session targets.
"""

from __future__ import annotations

import bisect
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import text

HORIZONS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "60m", "end_of_session")
_HORIZON_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
_DIRECTIONS = {"up", "down", "neutral"}
_MAX_LIMIT = 500
_MAX_LOOKBACK_MINUTES = 24 * 60
# Bounded sample cap for the pre-event volatility path: 2000 rows covers a
# 24-hour lookback at one-minute cadence (and multi-hour at tick cadence).
_VOLATILITY_SAMPLE_LIMIT = 2000
# Default lookback (trading minutes) for matched-horizon volatility samples.
# Independent of baseline_lookback_minutes: a 60m horizon needs at least two
# hours of samples per pair, so a short baseline lookback cannot feed it.
_DEFAULT_VOLATILITY_LOOKBACK_MINUTES = 24 * 60

# 1 = pre-044 per-bar realized volatility; 2 = timestamp-paired matched-horizon
# volatility (persisted in volatility_version).
_VOLATILITY_VERSION = 2

# A row is a usable price sample when its close is finite, or its close is
# NULL and its open is finite (mirrors _price). Non-finite values must be
# rejected in SQL so ORDER/LIMIT cannot pick a NaN/Infinity row and mask a
# later valid sample in Python.
_FINITE_PRICE_PREDICATE = """(
    COALESCE(close, open) IS NOT NULL
    AND (
        (close IS NOT NULL AND close NOT IN
            ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
             '-Infinity'::DOUBLE PRECISION))
        OR (close IS NULL AND open IS NOT NULL AND open NOT IN
            ('NaN'::DOUBLE PRECISION, 'Infinity'::DOUBLE PRECISION,
             '-Infinity'::DOUBLE PRECISION))
    )
)"""

# Baseline selection is the direct latest pre-event row (no range bound, so a
# dense series cannot starve or bias the pick). baseline_lookback_minutes is a
# post-selection freshness policy: an over-age baseline is recorded as
# stale_baseline in the row and provenance instead of changing this SQL.
_BASELINE_SQL = text(
    f"""SELECT timestamp, close, open, source FROM market_data
    WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp<:event_at
      AND {_FINITE_PRICE_PREDICATE}
    ORDER BY timestamp DESC LIMIT 1"""
)
_TARGET_SQL = text(
    f"""SELECT timestamp, close, open, source FROM market_data
    WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp>=:target_at AND timestamp<=:upper
      AND {_FINITE_PRICE_PREDICATE}
    ORDER BY timestamp ASC LIMIT 1"""
)
# end_of_session target: the FINAL eligible observation at or before the venue
# close (within a plain backward tolerance), never a post-close tick.
_EOS_TARGET_SQL = text(
    f"""SELECT timestamp, close, open, source FROM market_data
    WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp>=:lower AND timestamp<=:target_at
      AND {_FINITE_PRICE_PREDICATE}
    ORDER BY timestamp DESC LIMIT 1"""
)
# Pre-event volatility samples: time-stratified bucket-last downsampling
# across the FULL configured trading-time lookback (bounded by
# _VOLATILITY_SAMPLE_LIMIT buckets). Dense tick data therefore spans the whole
# window instead of only the latest N rows, so long horizons (1h/eos) still
# form historical pairs deterministically.
_PRE_SQL = text(
    f"""SELECT timestamp, close, open, source FROM (
        SELECT timestamp, close, open, source,
               ROW_NUMBER() OVER (
                   PARTITION BY FLOOR(EXTRACT(EPOCH FROM timestamp) / :bucket_seconds)
                   ORDER BY timestamp DESC
               ) AS bucket_rank
        FROM market_data
        WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp>=:lower AND timestamp<:event_at
          AND {_FINITE_PRICE_PREDICATE}
    ) stratified
    WHERE bucket_rank = 1
    ORDER BY timestamp ASC"""
)
# Post-event path classification: SQL aggregation of the exact classifier
# semantics (first/last nonzero sign plus presence of above/below vs the
# baseline) over the FULL event..upper interval — no row cap, so a late
# reversal or final sign is never missed.
_PATH_STATE_SQL = text(
    f"""WITH path AS (
        SELECT timestamp, close, open FROM market_data
        WHERE symbol=:symbol AND timeframe=:timeframe
          AND timestamp>=:lower AND timestamp<=:upper
          AND {_FINITE_PRICE_PREDICATE}
    )
    SELECT
        (SELECT CASE WHEN close > :baseline THEN 1 ELSE -1 END FROM path
         WHERE close <> :baseline ORDER BY timestamp ASC LIMIT 1) AS first_sign,
        (SELECT CASE WHEN close > :baseline THEN 1 ELSE -1 END FROM path
         WHERE close <> :baseline ORDER BY timestamp DESC LIMIT 1) AS last_sign,
        EXISTS(SELECT 1 FROM path WHERE close > :baseline) AS has_positive,
        EXISTS(SELECT 1 FROM path WHERE close < :baseline) AS has_negative"""
)
_UPDATE_SQL = text(
    """UPDATE event_reaction_windows SET
      target_at=:target_at, baseline_at=:baseline_at, baseline_price=:baseline_price,
      baseline_offset_seconds=:baseline_offset_seconds, target_price=:target_price,
      target_offset_seconds=:target_offset_seconds, observed_at=:observed_at,
      observed_price=:observed_price, absolute_move=:absolute_move,
      percentage_move=:percentage_move, volatility_adjusted_move=:volatility_adjusted_move,
      volatility_version=:volatility_version, direction_vs_expected=:direction_vs_expected,
      reaction_state=:reaction_state, missing_data_reason=:missing_data_reason,
      calendar_name=:calendar_name, calendar_version=:calendar_version,
      provenance=CAST(:provenance AS JSONB), updated_at=:updated_at
    WHERE id=:id"""
)


def _event_value(event: Any, key: str, default: Any = None) -> Any:
    return (
        event.get(key, default)
        if isinstance(event, Mapping)
        else getattr(event, key, default)
    )


def _payload(event: Any) -> Mapping[str, Any]:
    value = _event_value(event, "payload", {})
    return value if isinstance(value, Mapping) else {}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any, *, default: datetime | None = None) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return default
    else:
        return default
    if result.tzinfo is None or result.utcoffset() is None:
        return default
    return result.astimezone(UTC)


def _text_value(value: Any, *, max_length: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:max_length] if value else None


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value, depth=depth + 1)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _text_value(key, max_length=100)
            if key_text is not None:
                result[key_text] = _json_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth=depth + 1) for item in value[:100]]
    return None


def _normal_key(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        "_",
        _text_value(value, max_length=200).lower() if isinstance(value, str) else "",
    ).strip("_")


def _expected_direction(value: Any) -> str:
    key = _normal_key(value)
    if key in {"up", "higher", "rise", "rising", "positive"}:
        return "up"
    if key in {"down", "lower", "fall", "falling", "negative"}:
        return "down"
    if key in {"neutral", "flat", "unchanged", "stable", "none", "no_change"}:
        return "neutral"
    return "neutral"


def _mapping_entries(
    config: Mapping[str, Any] | None, event: Any
) -> list[dict[str, Any]]:
    """Resolve top-level macro_event_mappings keyed by series_id/event name."""
    if not isinstance(config, Mapping):
        return []
    mappings = config.get("macro_event_mappings", {})
    if not isinstance(mappings, Mapping):
        return []
    payload = _payload(event)
    candidates = (
        payload.get("series_id"),
        payload.get("event_name"),
        payload.get("name"),
        payload.get("title"),
        _event_value(event, "event_type"),
    )
    selected = selected_key = None
    normalized = {_normal_key(key): key for key in mappings}
    for candidate in candidates:
        key = normalized.get(_normal_key(candidate))
        if key is not None:
            selected, selected_key = mappings[key], key
            break
    if not isinstance(selected, Mapping):
        return []
    raw = selected.get("instruments", [])
    if isinstance(raw, Mapping):
        raw = list(raw)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence):
        return []
    sensitivities = selected.get("expected_sensitivity", {})
    sensitivities = sensitivities if isinstance(sensitivities, Mapping) else {}
    timeframe_default = (
        _text_value(config.get("oanda", {}).get("snapshot_timeframe"), max_length=30)
        if isinstance(config.get("oanda"), Mapping)
        else None
    )
    result: list[dict[str, Any]] = []
    for item in raw[:100]:
        if isinstance(item, Mapping):
            symbol = _text_value(
                item.get("symbol", item.get("instrument", item.get("canonical_id")))
            )
            local_sensitivity = (
                item.get("sensitivity", sensitivities.get(symbol)) if symbol else None
            )
            timeframe = (
                _text_value(item.get("timeframe"), max_length=30)
                or timeframe_default
                or "PRICE"
            )
            volatility = item.get("volatility")
            raw_mapping = item
        else:
            symbol = _text_value(item)
            local_sensitivity = sensitivities.get(symbol) if symbol else None
            timeframe = timeframe_default or "PRICE"
            volatility = None
            raw_mapping = {"symbol": symbol}
        if symbol is None:
            continue
        # Config uses positive/negative/neutral; preserve sensitivity separately.
        sensitivity_key = _normal_key(local_sensitivity)
        sensitivity = (
            sensitivity_key
            if sensitivity_key
            in {"positive", "negative", "neutral", "high", "moderate", "low"}
            else "neutral"
        )
        expected = _expected_direction(local_sensitivity)
        result.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "expected_direction": expected,
                "sensitivity": sensitivity,
                "mapping_key": _text_value(selected_key),
                "mapping": _json_value(
                    {
                        "event_name": selected.get("event_name"),
                        "priority": selected.get("priority"),
                        "source": raw_mapping,
                    }
                ),
                "volatility": _finite_number(volatility),
            }
        )
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in result:
        deduped.setdefault((item["symbol"], item["timeframe"]), item)
    return list(deduped.values())


def _event_time(event: Any) -> datetime | None:
    payload = _event_value(event, "payload", {})
    if isinstance(payload, Mapping):
        for key in ("released_at", "revision_at", "published_at"):
            if value := _timestamp(payload.get(key)):
                return value
    return _timestamp(_event_value(event, "effective_at")) or _timestamp(
        _event_value(event, "observed_at")
    )


def _reaction_settings(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    settings = (
        config.get("reaction_windows", {}) if isinstance(config, Mapping) else {}
    )
    return settings if isinstance(settings, Mapping) else {}


def _calendar_for(symbol: str | None, config: Mapping[str, Any] | None) -> Any:
    from venue_calendar import venue_for_symbol

    return venue_for_symbol(symbol, config)


def _instrument_policy(
    symbol: str | None,
    config: Mapping[str, Any] | None,
    *,
    default_timeframe: str,
) -> Any:
    from venue_calendar import instrument_policy_for

    return instrument_policy_for(symbol, config, default_timeframe=default_timeframe)


def _offset_seconds(at: datetime | None, event_at: datetime | None) -> int | None:
    """Whole-second offset from the event.

    Negative deltas are floored (toward -inf) so a strictly pre-event sample
    always persists as <= -1 second under the BIGINT column and the
    baseline_offset_sign_check is never violated by a subsecond rounding to
    zero; non-negative deltas round to nearest whole second.
    """
    if at is None or event_at is None:
        return None
    seconds = (at - event_at).total_seconds()
    if seconds < 0:
        return int(math.floor(seconds))
    return int(round(seconds))


def horizon_target(
    event_at: datetime,
    horizon: str,
    config: Mapping[str, Any] | None = None,
    *,
    symbol: str | None = None,
) -> datetime:
    """Target timestamp for a horizon, calendar-aware for end_of_session."""
    if horizon not in HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")
    if horizon == "end_of_session":
        return _calendar_for(symbol, config).session_close_after(event_at)
    return event_at + timedelta(minutes=_HORIZON_MINUTES[horizon])


def calculate_window_metrics(
    baseline_price: Any, target_price: Any, *, volatility: Any = None
) -> dict[str, Any]:
    """Return exact move metrics and an explicit missing/zero reason."""
    baseline = _finite_number(baseline_price)
    target = _finite_number(target_price)
    if baseline is None:
        return {
            "absolute_move": None,
            "percentage_move": None,
            "volatility_adjusted_move": None,
            "missing_data_reason": "missing_baseline",
        }
    if target is None:
        return {
            "absolute_move": None,
            "percentage_move": None,
            "volatility_adjusted_move": None,
            "missing_data_reason": "missing_target",
        }
    absolute = target - baseline
    if baseline == 0:
        return {
            "absolute_move": absolute,
            "percentage_move": None,
            "volatility_adjusted_move": None,
            "missing_data_reason": "zero_baseline",
        }
    if target == 0:
        return {
            "absolute_move": absolute,
            "percentage_move": None,
            "volatility_adjusted_move": None,
            "missing_data_reason": "zero_target",
        }
    percentage = absolute / baseline * 100.0
    vol = _finite_number(volatility)
    return {
        "absolute_move": absolute,
        "percentage_move": percentage,
        "volatility_adjusted_move": percentage / vol
        if vol is not None and vol > 0
        else None,
        "missing_data_reason": None,
    }


def classify_direction(
    actual_move: Any, expected_direction: Any, sensitivity: Any = None
) -> str:
    actual = _finite_number(actual_move)
    expected = _expected_direction(expected_direction)
    if actual is None or expected not in _DIRECTIONS:
        return "unknown"
    if actual == 0 or expected == "neutral":
        return "neutral"
    return "aligned" if (actual > 0) == (expected == "up") else "opposed"


def classify_reaction_state(prices: Sequence[Any], baseline_price: Any = None) -> str:
    """Classify a path as persistence, reversal, mixed, or pending."""
    baseline = _finite_number(baseline_price)
    values = [_finite_number(value) for value in prices]
    values = [value for value in values if value is not None]
    if baseline is None and values:
        baseline, values = values[0], values[1:]
    if baseline is None or not values:
        return "pending"
    signs = [
        1 if value > baseline else -1 if value < baseline else 0 for value in values
    ]
    signs = [sign for sign in signs if sign]
    if not signs:
        return "mixed"
    if len(set(signs)) == 1:
        return "persistence"
    return "reversal" if signs[0] != signs[-1] else "mixed"


def _rows(result: Any) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in result.mappings().all()]
    except AttributeError:
        try:
            return [dict(row) for row in result.all()]
        except AttributeError:
            return []


def _price(row: Mapping[str, Any]) -> float | None:
    value = row.get("close")
    return _finite_number(value if value is not None else row.get("open"))


def _baseline_row(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    event_at: datetime,
) -> dict[str, Any] | None:
    """Most recent row strictly before the event (direct latest, unbounded)."""
    rows = _rows(
        session.execute(
            _BASELINE_SQL,
            {"symbol": symbol, "timeframe": timeframe, "event_at": event_at},
        )
    )
    return rows[0] if rows else None


def _stale_baseline(
    baseline_at: datetime | None,
    event_at: datetime,
    lookback_minutes: int,
    calendar: Any,
) -> bool:
    """Freshness policy applied AFTER direct-latest selection.

    The baseline is stale when it predates the trading-time lower bound
    ``calendar.backward(event_at, lookback_minutes)``: wall-clock gaps across
    weekends/holidays carry no trading time, so a Friday-close baseline for a
    Monday-open event is fresh when the trading-time distance is within the
    lookback. The wall-clock offset is still persisted separately.
    """
    if baseline_at is None:
        return False
    lower = calendar.backward(event_at, lookback_minutes)
    return baseline_at < lower


def _target_row(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    target_at: datetime,
    upper: datetime,
) -> dict[str, Any] | None:
    """First row at or after the target within the tolerance bound."""
    rows = _rows(
        session.execute(
            _TARGET_SQL,
            {"symbol": symbol, "timeframe": timeframe, "target_at": target_at, "upper": upper},
        )
    )
    return rows[0] if rows else None


def _eos_target_row(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    target_at: datetime,
    lower: datetime,
) -> dict[str, Any] | None:
    """Final row at or before the session close within the backward tolerance.

    Never selects a post-close tick: the venue close was computed by the
    calendar and the bound is a plain wall-clock ``target_at - tolerance``.
    """
    rows = _rows(
        session.execute(
            _EOS_TARGET_SQL,
            {"symbol": symbol, "timeframe": timeframe, "target_at": target_at, "lower": lower},
        )
    )
    return rows[0] if rows else None


def _pre_rows(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    lower: datetime,
    event_at: datetime,
    bucket_seconds: int,
) -> list[dict[str, Any]]:
    """Bounded, time-stratified pre-event samples (volatility input).

    One (last) sample per ``bucket_seconds`` bucket across the full lookback,
    so the result spans lower..event bounded by ~_VOLATILITY_SAMPLE_LIMIT
    buckets regardless of tick density."""
    return _rows(
        session.execute(
            _PRE_SQL,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "lower": lower,
                "event_at": event_at,
                "bucket_seconds": max(1, int(bucket_seconds)),
            },
        )
    )


def _classify_path_state(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    lower: datetime,
    upper: datetime,
    baseline_price: float,
) -> str:
    """Classify the post-event path against the baseline in SQL (exact
    classifier semantics over the full interval; no row cap)."""
    rows = _rows(
        session.execute(
            _PATH_STATE_SQL,
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "lower": lower,
                "upper": upper,
                "baseline": baseline_price,
            },
        )
    )
    if not rows:
        return "mixed"
    first = rows[0].get("first_sign")
    last = rows[0].get("last_sign")
    has_positive = bool(rows[0].get("has_positive"))
    has_negative = bool(rows[0].get("has_negative"))
    if first is None:
        return "mixed"
    if has_positive != has_negative:  # exactly one sign present
        return "persistence"
    return "reversal" if first != last else "mixed"


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _realized_volatility(
    rows: Sequence[Mapping[str, Any]],
    *,
    horizon_seconds: int,
    horizon_minutes: int | None = None,
    lookback_minutes: int | None = None,
    bucket_seconds: int = 1,
    timeframe: str = "PRICE",
) -> tuple[float | None, dict[str, Any]]:
    """Matched-horizon realized volatility and calculation provenance.

    Returns are timestamp-paired at the actual horizon: starting from a
    non-overlapping anchor sample, each return pairs the anchor price with the
    price of the sample whose timestamp is closest to ``anchor + horizon``
    (first sample at or beyond the target, within a cadence-derived tolerance
    band of one full cadence so time-stratified bucket-last samples always
    resolve their bucket). The population standard deviation of those returns
    is the estimate. When fewer than two horizon-paired returns exist the
    result is None (no scaling or normalization is applied), except when the
    observed cadence equals the horizon, where consecutive per-bar returns are
    themselves same-horizon returns. ``horizon_seconds`` is the actual
    event_at -> target_at interval (for end_of_session this is the real
    session duration); ``bucket_seconds`` records the pre-event
    time-stratified downsampling width used by the caller.
    """
    priced: list[tuple[datetime, float]] = []
    for row in rows:
        ts = _timestamp(row.get("timestamp"))
        price = _price(row)
        if ts is None or price is None or price <= 0:
            continue
        priced.append((ts, price))
    meta: dict[str, Any] = {
        "version": _VOLATILITY_VERSION,
        "method": "insufficient_samples",
        "horizon_seconds": horizon_seconds,
        "horizon_minutes": horizon_minutes,
        "lookback_minutes": lookback_minutes,
        "sampling_method": "bucket_last",
        "downsample_bucket_seconds": max(1, int(bucket_seconds)),
        "source_timeframe": timeframe,
        "observation_interval_seconds": None,
        "samples": len(priced),
        "returns": 0,
        "lookback_start": None,
        "lookback_end": None,
    }
    if len(priced) < 2:
        return None, meta
    meta["lookback_start"] = _json_value(priced[0][0])
    meta["lookback_end"] = _json_value(priced[-1][0])
    gaps = [
        (priced[index][0] - priced[index - 1][0]).total_seconds()
        for index in range(1, len(priced))
        if (priced[index][0] - priced[index - 1][0]).total_seconds() > 0
    ]
    cadence = _median(gaps) if gaps else 0.0
    meta["observation_interval_seconds"] = int(round(cadence))
    # One full cadence of tolerance: a time-stratified bucket-last sample's
    # bucket is always within one bucket width of any target inside it.
    tolerance = max(1.0, cadence)
    timestamps = [priced[index][0] for index in range(len(priced))]
    returns: list[float] = []
    anchor_index = 0
    while anchor_index < len(priced) - 1:
        anchor_ts, anchor_price = priced[anchor_index]
        target_ts = anchor_ts + timedelta(seconds=horizon_seconds)
        upper = target_ts + timedelta(seconds=tolerance)
        # First at-or-after pairing via monotonic bisect (no full-series
        # scan): the return's end sample is the FIRST sample at or after the
        # target within the cadence-derived tolerance band, so every pair
        # spans at least a full horizon (no under-horizon returns).
        pos = bisect.bisect_left(timestamps, target_ts, anchor_index + 1)
        if pos >= len(priced) or timestamps[pos] > upper:
            anchor_index += 1
            continue
        returns.append((priced[pos][1] / anchor_price - 1.0) * 100.0)
        anchor_index = pos  # non-overlapping anchors
    method = "first_at_or_after"
    if len(returns) < 2 and abs(cadence - horizon_seconds) <= tolerance:
        # The cadence equals the horizon: consecutive per-bar returns are
        # themselves same-horizon returns.
        method = "per_bar_cadence_matched"
        returns = [
            (priced[index][1] / priced[index - 1][1] - 1.0) * 100.0
            for index in range(1, len(priced))
        ]
    elif len(returns) < 2:
        method = "insufficient_pairs"
    if not returns:
        meta["method"] = method
        return None, meta
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    volatility = math.sqrt(variance)
    meta.update(
        {
            "method": method,
            "returns": len(returns),
        }
    )
    if not (math.isfinite(volatility) and volatility > 0):
        return None, meta
    return volatility, meta


def _row_provenance(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("provenance") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    provenance = _json_value(raw)
    return provenance if isinstance(provenance, dict) else {}


def _initial_record(
    event: Any,
    entry: Mapping[str, Any],
    horizon: str,
    *,
    now: datetime,
    config: Mapping[str, Any],
    baseline_row: Mapping[str, Any] | None,
    policy: Any,
    calendar: Any,
    lookback: int,
) -> dict[str, Any]:
    event_at = _event_time(event)
    if event_at is None:
        raise ValueError("event requires an aware timestamp")
    event_id = _event_value(event, "event_id", _event_value(event, "id"))
    target_at = horizon_target(event_at, horizon, config, symbol=entry["symbol"])
    baseline_price = _price(baseline_row) if baseline_row else None
    baseline_at = (
        _timestamp(baseline_row.get("timestamp")) if baseline_row else None
    )
    missing_reason = (
        "future_window"
        if target_at > now
        else "missing_baseline"
        if baseline_price is None
        else "zero_baseline"
        if baseline_price == 0
        else "stale_baseline"
        if _stale_baseline(baseline_at, event_at, lookback, calendar)
        else None
    )
    return {
        "event_id": event_id,
        "instrument_symbol": entry["symbol"],
        "timeframe": entry["timeframe"],
        "horizon": horizon,
        "event_at": event_at,
        "baseline_at": baseline_at,
        "target_at": target_at,
        "baseline_price": baseline_price,
        "target_price": None,
        "baseline_offset_seconds": _offset_seconds(baseline_at, event_at),
        "target_offset_seconds": None,
        "observed_at": None,
        "observed_price": None,
        "absolute_move": None,
        "percentage_move": None,
        "volatility_adjusted_move": None,
        "expected_direction": entry["expected_direction"],
        "sensitivity": entry["sensitivity"],
        "direction_vs_expected": "unknown",
        "reaction_state": "pending",
        "missing_data_reason": missing_reason,
        "calendar_name": policy.venue,
        "calendar_version": policy.calendar_version,
        "volatility_version": None,
        "source_payload": _json_value(_payload(event)),
        "provenance": _json_value(
            {
                "mapping_key": entry.get("mapping_key"),
                "mapping": entry.get("mapping"),
                "source_event_id": _event_value(event, "source_event_id"),
                "baseline_lookback_minutes": int(
                    min(_MAX_LOOKBACK_MINUTES, max(1, lookback))
                ),
                "baseline_source": _json_value(baseline_row.get("source"))
                if baseline_row
                else None,
                "calendar": _json_value(policy.to_metadata()),
            }
        ),
        "created_at": now,
        "updated_at": now,
    }


def initialize_reaction_windows(
    session: Any,
    event: Any,
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    config = config if isinstance(config, Mapping) else {}
    current = _timestamp(now, default=datetime.now(UTC))
    event_id = _event_value(event, "event_id", _event_value(event, "id"))
    entries = _mapping_entries(config, event)
    event_at = _event_time(event)
    if event_id is None or event_at is None:
        raise ValueError("event_id and an aware event timestamp are required")
    created = existing = 0
    settings = _reaction_settings(config)
    lookback = min(
        _MAX_LOOKBACK_MINUTES,
        max(1, _finite_number(settings.get("baseline_lookback_minutes", 120)) or 120),
    )
    for entry in entries:
        symbol = entry["symbol"]
        policy = _instrument_policy(
            symbol, config, default_timeframe=entry["timeframe"]
        )
        feed_timeframe = policy.price_timeframe
        calendar = _calendar_for(symbol, config)
        baseline_row = _baseline_row(
            session,
            symbol=symbol,
            timeframe=feed_timeframe,
            event_at=event_at,
        )
        for horizon in HORIZONS:
            record = _initial_record(
                event,
                entry,
                horizon,
                now=current,
                config=config,
                baseline_row=baseline_row,
                policy=policy,
                calendar=calendar,
                lookback=lookback,
            )
            result = session.execute(
                text(
                    """INSERT INTO event_reaction_windows
                    (event_id,instrument_symbol,timeframe,horizon,event_at,baseline_at,target_at,baseline_price,target_price,baseline_offset_seconds,target_offset_seconds,observed_at,observed_price,absolute_move,percentage_move,volatility_adjusted_move,expected_direction,sensitivity,direction_vs_expected,reaction_state,missing_data_reason,calendar_name,calendar_version,volatility_version,source_payload,provenance,created_at,updated_at)
                    VALUES (:event_id,:instrument_symbol,:timeframe,:horizon,:event_at,:baseline_at,:target_at,:baseline_price,:target_price,:baseline_offset_seconds,:target_offset_seconds,:observed_at,:observed_price,:absolute_move,:percentage_move,:volatility_adjusted_move,:expected_direction,:sensitivity,:direction_vs_expected,:reaction_state,:missing_data_reason,:calendar_name,:calendar_version,:volatility_version,CAST(:source_payload AS JSONB),CAST(:provenance AS JSONB),:created_at,:updated_at)
                    ON CONFLICT (event_id,instrument_symbol,timeframe,horizon) DO NOTHING"""
                ),
                {
                    **record,
                    "source_payload": json.dumps(
                        record["source_payload"], sort_keys=True
                    ),
                    "provenance": json.dumps(record["provenance"], sort_keys=True),
                },
            )
            if getattr(result, "rowcount", 1) == 0:
                existing += 1
            else:
                created += 1
    return {
        "event_id": str(event_id),
        "mapped_instruments": len(entries),
        "horizons": len(HORIZONS),
        "created": created,
        "existing": existing,
        "total": created + existing,
    }


def _pending_rows(session: Any, *, now: datetime, limit: int) -> list[dict[str, Any]]:
    result = session.execute(
        text(
            """SELECT id,event_id,instrument_symbol,timeframe,horizon,event_at,target_at,baseline_at,baseline_price,target_price,observed_at,observed_price,expected_direction,sensitivity,reaction_state,missing_data_reason,provenance
            FROM event_reaction_windows WHERE (target_at IS NULL OR target_at<=:now)
              AND (observed_price IS NULL OR missing_data_reason IS NOT NULL)
            ORDER BY target_at NULLS FIRST,id LIMIT :limit"""
        ),
        {"now": now, "limit": max(1, min(int(limit), _MAX_LIMIT))},
    )
    return _rows(result)


def _resolve_window(
    session: Any,
    row: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    event_at: datetime,
    target_at: datetime,
    current: datetime,
) -> tuple[dict[str, Any], str]:
    """Derive baseline/target/volatility/metrics for one window.

    Returns ``(updates, outcome)`` where ``updates`` is a complete UPDATE
    parameter mapping and ``outcome`` is one of resolved / missing_baseline /
    zero_baseline / stale_baseline / missing_target. Shared by backfill and
    recompute so both paths always apply identical selection rules.
    """
    symbol = str(row["instrument_symbol"])
    timeframe = str(row.get("timeframe") or "PRICE")
    policy = _instrument_policy(symbol, config, default_timeframe=timeframe)
    feed_timeframe = policy.price_timeframe
    calendar = _calendar_for(symbol, config)
    settings = _reaction_settings(config)
    lookback = min(
        _MAX_LOOKBACK_MINUTES,
        max(1, _finite_number(settings.get("baseline_lookback_minutes", 120)) or 120),
    )
    volatility_lookback = min(
        _MAX_LOOKBACK_MINUTES,
        max(
            1,
            _finite_number(
                settings.get("volatility_lookback_minutes", 24 * 60)
            )
            or 24 * 60,
        ),
    )
    configured_tolerance = _finite_number(
        settings.get("target_tolerance_minutes", 5)
    )
    tolerance = (
        5
        if configured_tolerance is None
        else min(1440, max(0, configured_tolerance))
    )
    tolerance_delta = timedelta(minutes=tolerance)
    volatility_lower = calendar.backward(event_at, volatility_lookback)
    bucket_seconds = max(
        1, math.ceil(volatility_lookback * 60 / _VOLATILITY_SAMPLE_LIMIT)
    )
    horizon = str(row.get("horizon") or "1m")
    if horizon == "end_of_session":
        # The calendar computed the venue close; the target is the FINAL
        # observation at or before it (plain backward tolerance) and the path
        # window never extends past the close.
        upper = target_at
        target_row = _eos_target_row(
            session,
            symbol=symbol,
            timeframe=feed_timeframe,
            target_at=target_at,
            lower=target_at - tolerance_delta,
        )
    else:
        # Plain wall-clock tolerance: never crosses a closure/weekend, so a
        # Monday tick can never satisfy a Friday target.
        upper = target_at + tolerance_delta
        target_row = _target_row(
            session,
            symbol=symbol,
            timeframe=feed_timeframe,
            target_at=target_at,
            upper=upper,
        )
    baseline_row = _baseline_row(
        session, symbol=symbol, timeframe=feed_timeframe, event_at=event_at
    )
    pre_rows = _pre_rows(
        session,
        symbol=symbol,
        timeframe=feed_timeframe,
        lower=volatility_lower,
        event_at=event_at,
        bucket_seconds=bucket_seconds,
    )
    baseline_price = (
        _price(baseline_row)
        if baseline_row
        else _finite_number(row.get("baseline_price"))
    )
    baseline_at = (
        _timestamp(baseline_row.get("timestamp"))
        if baseline_row
        else _timestamp(row.get("baseline_at"))
    )
    baseline_offset = _offset_seconds(baseline_at, event_at)
    provenance = _row_provenance(row)
    updates: dict[str, Any] = {
        "calendar_name": calendar.name,
        "calendar_version": calendar.version,
        "baseline_at": baseline_at,
        "baseline_price": baseline_price,
        "baseline_offset_seconds": baseline_offset,
        "target_price": None,
        "target_offset_seconds": None,
        "observed_at": None,
        "observed_price": None,
        "absolute_move": None,
        "percentage_move": None,
        "volatility_adjusted_move": None,
        "volatility_version": None,
        "direction_vs_expected": "unknown",
        "reaction_state": "pending",
        "missing_data_reason": None,
        "provenance": json.dumps(provenance, sort_keys=True),
    }
    if baseline_price is None:
        updates["missing_data_reason"] = "missing_baseline"
        return updates, "missing_baseline"
    if baseline_price == 0:
        updates["missing_data_reason"] = "zero_baseline"
        return updates, "zero_baseline"
    if _stale_baseline(baseline_at, event_at, lookback, calendar):
        # Direct-latest selection found a baseline older than the freshness
        # policy; keep it for audit but leave the window unresolved.
        stale = {
            "max_age_minutes": lookback,
            "age_minutes": int(
                (event_at - baseline_at).total_seconds() // 60
            ),
            "baseline_at": _json_value(baseline_at),
        }
        stale_provenance = dict(provenance)
        stale_provenance["stale_baseline"] = stale
        updates["provenance"] = json.dumps(stale_provenance, sort_keys=True)
        updates["missing_data_reason"] = "stale_baseline"
        return updates, "stale_baseline"
    observed_at = _timestamp(target_row.get("timestamp")) if target_row else None
    target_price = _price(target_row) if target_row else None
    updates.update(
        {
            "target_price": target_price,
            # Offset of the observed sample relative to the planned target
            # (operationally the realized delay; may be negative under
            # pre-event target policies).
            "target_offset_seconds": _offset_seconds(observed_at, target_at),
            "observed_at": observed_at,
            "observed_price": target_price,
        }
    )
    if target_price is None:
        updates["missing_data_reason"] = "missing_target"
        return updates, "missing_target"
    horizon_minutes = _HORIZON_MINUTES.get(horizon)
    # The pairing horizon is the actual event_at -> target_at interval (for
    # end_of_session this is the real session duration, not a nominal 1m).
    horizon_seconds = max(
        1, int(math.ceil((target_at - event_at).total_seconds()))
    )
    volatility, volatility_meta = _realized_volatility(
        pre_rows,
        horizon_seconds=horizon_seconds,
        horizon_minutes=horizon_minutes,
        lookback_minutes=volatility_lookback,
        bucket_seconds=bucket_seconds,
        timeframe=feed_timeframe,
    )
    metrics = calculate_window_metrics(
        baseline_price, target_price, volatility=volatility
    )
    enriched = dict(provenance)
    enriched.update(
        {
            "baseline_source": _json_value(baseline_row.get("source"))
            if baseline_row
            else None,
            "target_source": _json_value(target_row.get("source"))
            if target_row
            else None,
            "backfilled_at": _json_value(current),
            "market_data_window": {
                "from": _json_value(event_at),
                "to": _json_value(upper),
            },
            "calendar": _json_value(policy.to_metadata()),
            "volatility": _json_value(volatility_meta),
        }
    )
    updates.update(
        {
            **metrics,
            "volatility_version": _VOLATILITY_VERSION,
            "direction_vs_expected": classify_direction(
                metrics["percentage_move"],
                row.get("expected_direction"),
                row.get("sensitivity"),
            ),
            "reaction_state": _classify_path_state(
                session,
                symbol=symbol,
                timeframe=feed_timeframe,
                lower=event_at,
                upper=upper,
                baseline_price=baseline_price,
            ),
            "missing_data_reason": None,
            "provenance": json.dumps(enriched, sort_keys=True),
        }
    )
    return updates, "resolved"


def _apply_window_update(
    session: Any,
    row_id: Any,
    target_at: datetime,
    updates: Mapping[str, Any],
    current: datetime,
) -> None:
    session.execute(
        _UPDATE_SQL,
        {"id": row_id, "target_at": target_at, **updates, "updated_at": current},
    )


def backfill_reaction_windows(
    session: Any,
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    config = config if isinstance(config, Mapping) else {}
    current = _timestamp(now, default=datetime.now(UTC))
    pending = _pending_rows(session, now=current, limit=limit)
    completed = unresolved = skipped_future = 0
    for row in pending:
        target_at = _timestamp(row.get("target_at"))
        if target_at is None or target_at > current:
            skipped_future += 1
            continue
        event_at = _timestamp(row.get("event_at"))
        if event_at is None:
            unresolved += 1
            continue
        updates, outcome = _resolve_window(
            session,
            row,
            config,
            event_at=event_at,
            target_at=target_at,
            current=current,
        )
        _apply_window_update(session, row["id"], target_at, updates, current)
        if outcome == "resolved":
            completed += 1
        else:
            unresolved += 1
    return {
        "scanned": len(pending),
        "completed": completed,
        "unresolved": unresolved,
        "skipped_future": skipped_future,
        "limit": max(1, min(int(limit), _MAX_LIMIT)),
    }


def recompute_reaction_windows(
    session: Any,
    config: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    limit: int = 100,
    event_id: Any = None,
    legacy_only: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-derive existing windows with current selection and calendar rules.

    Unlike backfill, recompute refreshes every selected row (bounded by
    ``limit``, optionally scoped to one ``event_id``) and re-derives
    ``target_at`` — useful after calendar or volatility-version changes.

    ``legacy_only`` selects completed rows whose ``volatility_version`` is NULL
    or below the current calculation version (pre-044 rows resolved under the
    old per-bar volatility). Pending current-version rows legitimately have no
    volatility version and are excluded. Legacy rows are never silently
    relabeled by normal backfill/reconciliation; recompute is the explicit,
    operator-initiated path that re-derives them with ``_resolve_window``
    semantics and stores the current version. ``dry_run`` reports what would
    change without updating.
    """
    config = config if isinstance(config, Mapping) else {}
    current = _timestamp(now, default=datetime.now(UTC))
    bounded = max(1, min(int(limit), _MAX_LIMIT))
    legacy_filter = (
        " AND observed_price IS NOT NULL "
        "AND (volatility_version IS NULL OR volatility_version < "
        f"{_VOLATILITY_VERSION})"
        if legacy_only
        else ""
    )
    rows = _rows(
        session.execute(
            text(
                """SELECT id,event_id,instrument_symbol,timeframe,horizon,event_at,target_at,baseline_at,baseline_price,target_price,observed_at,observed_price,expected_direction,sensitivity,reaction_state,missing_data_reason,provenance
                FROM event_reaction_windows
                WHERE (:event_id IS NULL OR event_id=:event_id)"""
                + legacy_filter
                + " ORDER BY target_at NULLS FIRST,id LIMIT :limit"
            ),
            {"event_id": event_id, "limit": bounded},
        )
    )
    completed = unresolved = 0
    for row in rows:
        event_at = _timestamp(row.get("event_at"))
        if event_at is None:
            unresolved += 1
            continue
        symbol = str(row["instrument_symbol"])
        horizon = str(row.get("horizon") or "1m")
        target_at = horizon_target(event_at, horizon, config, symbol=symbol)
        updates, outcome = _resolve_window(
            session,
            row,
            config,
            event_at=event_at,
            target_at=target_at,
            current=current,
        )
        if not dry_run:
            _apply_window_update(session, row["id"], target_at, updates, current)
        if outcome == "resolved":
            completed += 1
        else:
            unresolved += 1
    return {
        "scanned": len(rows),
        "completed": completed,
        "unresolved": unresolved,
        "legacy_only": bool(legacy_only),
        "dry_run": bool(dry_run),
        "limit": bounded,
    }


def list_event_reactions(
    session: Any, event_id: Any, *, limit: int = 100
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), _MAX_LIMIT))
    result = session.execute(
        text(
            """SELECT event_id,instrument_symbol,timeframe,horizon,event_at,baseline_at,target_at,baseline_price,target_price,baseline_offset_seconds,target_offset_seconds,observed_at,observed_price,absolute_move,percentage_move,volatility_adjusted_move,expected_direction,sensitivity,direction_vs_expected,reaction_state,missing_data_reason,calendar_name,calendar_version,volatility_version,provenance,created_at,updated_at
            FROM event_reaction_windows WHERE event_id=:event_id ORDER BY instrument_symbol,timeframe,horizon LIMIT :limit"""
        ),
        {"event_id": event_id, "limit": bounded},
    )
    return _rows(result)


__all__ = [
    "HORIZONS",
    "backfill_reaction_windows",
    "calculate_window_metrics",
    "classify_direction",
    "classify_reaction_state",
    "horizon_target",
    "initialize_reaction_windows",
    "list_event_reactions",
    "recompute_reaction_windows",
]
