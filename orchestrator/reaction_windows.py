"""Deterministic market-event reaction-window persistence.

Functions use a caller-owned SQLAlchemy session and never commit. Mapping,
calculation, and database lookup are deliberately bounded and deterministic.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import text

HORIZONS: tuple[str, ...] = ("1m", "5m", "15m", "30m", "60m", "end_of_session")
_HORIZON_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
_DIRECTIONS = {"up", "down", "neutral"}
_MAX_LIMIT = 500
_MAX_LOOKBACK_MINUTES = 24 * 60


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


def _session_target(event_at: datetime, config: Mapping[str, Any] | None) -> datetime:
    settings = config.get("reaction_windows", {}) if isinstance(config, Mapping) else {}
    settings = settings if isinstance(settings, Mapping) else {}
    value = settings.get(
        "session_close",
        config.get("session_close") if isinstance(config, Mapping) else None,
    )
    close = time(23, 59, 59)
    if isinstance(value, str):
        try:
            close = time.fromisoformat(value)
        except ValueError:
            pass
    target = datetime.combine(event_at.date(), close, tzinfo=UTC)
    return target if target > event_at else target + timedelta(days=1)


def horizon_target(
    event_at: datetime, horizon: str, config: Mapping[str, Any] | None = None
) -> datetime:
    if horizon not in HORIZONS:
        raise ValueError(f"unsupported horizon: {horizon}")
    return (
        _session_target(event_at, config)
        if horizon == "end_of_session"
        else event_at + timedelta(minutes=_HORIZON_MINUTES[horizon])
    )


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


def _market_rows(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    lower: datetime,
    upper: datetime,
    limit: int = 500,
) -> list[dict[str, Any]]:
    result = session.execute(
        text("""SELECT timestamp, close, open, source FROM market_data
        WHERE symbol=:symbol AND timeframe=:timeframe AND timestamp>=:lower AND timestamp<=:upper
          AND COALESCE(close, open) IS NOT NULL ORDER BY timestamp ASC LIMIT :limit"""),
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "lower": lower,
            "upper": upper,
            "limit": max(1, min(int(limit), _MAX_LIMIT)),
        },
    )
    return _rows(result)


def _price(row: Mapping[str, Any]) -> float | None:
    value = row.get("close")
    return _finite_number(value if value is not None else row.get("open"))


def _realized_volatility(rows: Sequence[Mapping[str, Any]]) -> float | None:
    prices = [_price(row) for row in rows]
    prices = [price for price in prices if price is not None and price > 0]
    returns = [
        (prices[index] / prices[index - 1] - 1.0) * 100.0
        for index in range(1, len(prices))
    ]
    if not returns:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    volatility = math.sqrt(variance)
    return volatility if math.isfinite(volatility) and volatility > 0 else None


def _initial_record(
    event: Any,
    entry: Mapping[str, Any],
    horizon: str,
    *,
    now: datetime,
    config: Mapping[str, Any],
    baseline_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    event_at = _event_time(event)
    if event_at is None:
        raise ValueError("event requires an aware timestamp")
    event_id = _event_value(event, "event_id", _event_value(event, "id"))
    target_at = horizon_target(event_at, horizon, config)
    settings = config.get("reaction_windows", {})
    settings = settings if isinstance(settings, Mapping) else {}
    lookback = _finite_number(settings.get("baseline_lookback_minutes", 120)) or 120
    baseline_price = _price(baseline_row) if baseline_row else None
    missing_reason = (
        "future_window"
        if target_at > now
        else "missing_baseline"
        if baseline_price is None
        else "zero_baseline"
        if baseline_price == 0
        else None
    )
    return {
        "event_id": event_id,
        "instrument_symbol": entry["symbol"],
        "timeframe": entry["timeframe"],
        "horizon": horizon,
        "event_at": event_at,
        "baseline_at": _timestamp(baseline_row.get("timestamp"))
        if baseline_row
        else None,
        "target_at": target_at,
        "baseline_price": baseline_price,
        "target_price": None,
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
    settings = config.get("reaction_windows", {})
    settings = settings if isinstance(settings, Mapping) else {}
    lookback = min(
        _MAX_LOOKBACK_MINUTES,
        max(1, _finite_number(settings.get("baseline_lookback_minutes", 120)) or 120),
    )
    for entry in entries:
        baseline_rows = _market_rows(
            session,
            symbol=entry["symbol"],
            timeframe=entry["timeframe"],
            lower=event_at - timedelta(minutes=lookback),
            upper=event_at,
            limit=100,
        )
        baseline_row = baseline_rows[-1] if baseline_rows else None
        for horizon in HORIZONS:
            record = _initial_record(
                event,
                entry,
                horizon,
                now=current,
                config=config,
                baseline_row=baseline_row,
            )
            result = session.execute(
                text("""INSERT INTO event_reaction_windows
                (event_id,instrument_symbol,timeframe,horizon,event_at,baseline_at,target_at,baseline_price,target_price,observed_at,observed_price,absolute_move,percentage_move,volatility_adjusted_move,expected_direction,sensitivity,direction_vs_expected,reaction_state,missing_data_reason,source_payload,provenance,created_at,updated_at)
                VALUES (:event_id,:instrument_symbol,:timeframe,:horizon,:event_at,:baseline_at,:target_at,:baseline_price,:target_price,:observed_at,:observed_price,:absolute_move,:percentage_move,:volatility_adjusted_move,:expected_direction,:sensitivity,:direction_vs_expected,:reaction_state,:missing_data_reason,CAST(:source_payload AS JSONB),CAST(:provenance AS JSONB),:created_at,:updated_at)
                ON CONFLICT (event_id,instrument_symbol,horizon) DO NOTHING"""),
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
        text("""SELECT id,event_id,instrument_symbol,timeframe,horizon,event_at,target_at,baseline_at,baseline_price,target_price,observed_at,observed_price,expected_direction,sensitivity,reaction_state,missing_data_reason,provenance
        FROM event_reaction_windows WHERE (target_at IS NULL OR target_at<=:now)
          AND (observed_price IS NULL OR missing_data_reason IS NOT NULL)
        ORDER BY target_at NULLS FIRST,id LIMIT :limit"""),
        {"now": now, "limit": max(1, min(int(limit), _MAX_LIMIT))},
    )
    return _rows(result)


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
    settings = config.get("reaction_windows", {})
    settings = settings if isinstance(settings, Mapping) else {}
    lookback = min(
        _MAX_LOOKBACK_MINUTES,
        max(1, _finite_number(settings.get("baseline_lookback_minutes", 120)) or 120),
    )
    tolerance = min(
        60, max(1, _finite_number(settings.get("target_tolerance_minutes", 5)) or 5)
    )
    for row in pending:
        target_at = _timestamp(row.get("target_at"))
        if target_at is None or target_at > current:
            skipped_future += 1
            continue
        event_at = _timestamp(row.get("event_at"))
        if event_at is None:
            unresolved += 1
            continue
        symbol, timeframe = (
            str(row["instrument_symbol"]),
            str(row.get("timeframe") or "PRICE"),
        )
        baseline_rows = _market_rows(
            session,
            symbol=symbol,
            timeframe=timeframe,
            lower=event_at - timedelta(minutes=lookback),
            upper=event_at,
            limit=100,
        )
        path_rows = _market_rows(
            session,
            symbol=symbol,
            timeframe=timeframe,
            lower=event_at,
            upper=target_at + timedelta(minutes=tolerance),
            limit=500,
        )
        target_candidates = [
            item
            for item in path_rows
            if (_timestamp(item.get("timestamp")) or target_at) >= target_at
        ]
        target_row = target_candidates[0] if target_candidates else None
        baseline_row = baseline_rows[-1] if baseline_rows else None
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
        if baseline_price is None:
            unresolved += 1
            session.execute(
                text(
                    "UPDATE event_reaction_windows SET baseline_at=:baseline_at,baseline_price=NULL,missing_data_reason='missing_baseline',updated_at=:updated_at WHERE id=:id"
                ),
                {"id": row["id"], "baseline_at": baseline_at, "updated_at": current},
            )
            continue
        if baseline_price == 0:
            unresolved += 1
            session.execute(
                text(
                    "UPDATE event_reaction_windows SET baseline_at=:baseline_at,baseline_price=:baseline_price,missing_data_reason='zero_baseline',updated_at=:updated_at WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "baseline_at": baseline_at,
                    "baseline_price": baseline_price,
                    "updated_at": current,
                },
            )
            continue
        target_price = _price(target_row) if target_row else None
        if target_price is None:
            unresolved += 1
            session.execute(
                text(
                    "UPDATE event_reaction_windows SET baseline_at=:baseline_at,baseline_price=:baseline_price,missing_data_reason='missing_target',updated_at=:updated_at WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "baseline_at": baseline_at,
                    "baseline_price": baseline_price,
                    "updated_at": current,
                },
            )
            continue
        pre_event_rows = [
            item
            for item in baseline_rows
            if (_timestamp(item.get("timestamp")) or event_at) < event_at
        ]
        volatility = _realized_volatility(pre_event_rows)
        metrics = calculate_window_metrics(
            baseline_price, target_price, volatility=volatility
        )
        state = classify_reaction_state(
            [_price(item) for item in path_rows], baseline_price
        )
        raw_provenance = row.get("provenance") or {}
        if isinstance(raw_provenance, str):
            try:
                raw_provenance = json.loads(raw_provenance)
            except (TypeError, ValueError):
                raw_provenance = {}
        provenance = _json_value(raw_provenance)
        provenance = provenance if isinstance(provenance, dict) else {}
        provenance.update(
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
                    "to": _json_value(target_at + timedelta(minutes=tolerance)),
                },
            }
        )
        session.execute(
            text(
                """UPDATE event_reaction_windows SET baseline_at=:baseline_at,baseline_price=:baseline_price,target_price=:target_price,observed_at=:observed_at,observed_price=:observed_price,absolute_move=:absolute_move,percentage_move=:percentage_move,volatility_adjusted_move=:volatility_adjusted_move,direction_vs_expected=:direction_vs_expected,reaction_state=:reaction_state,missing_data_reason=:missing_data_reason,provenance=CAST(:provenance AS JSONB),updated_at=:updated_at WHERE id=:id"""
            ),
            {
                "id": row["id"],
                "baseline_at": baseline_at,
                "baseline_price": baseline_price,
                "target_price": target_price,
                "observed_at": _timestamp(target_row.get("timestamp")),
                "observed_price": target_price,
                **metrics,
                "direction_vs_expected": classify_direction(
                    metrics["percentage_move"],
                    row.get("expected_direction"),
                    row.get("sensitivity"),
                ),
                "reaction_state": state,
                "provenance": json.dumps(provenance, sort_keys=True),
                "updated_at": current,
            },
        )
        completed += 1
    return {
        "scanned": len(pending),
        "completed": completed,
        "unresolved": unresolved,
        "skipped_future": skipped_future,
        "limit": max(1, min(int(limit), _MAX_LIMIT)),
    }


def list_event_reactions(
    session: Any, event_id: Any, *, limit: int = 100
) -> list[dict[str, Any]]:
    bounded = max(1, min(int(limit), _MAX_LIMIT))
    result = session.execute(
        text(
            """SELECT event_id,instrument_symbol,timeframe,horizon,event_at,baseline_at,target_at,baseline_price,target_price,observed_at,observed_price,absolute_move,percentage_move,volatility_adjusted_move,expected_direction,sensitivity,direction_vs_expected,reaction_state,missing_data_reason,provenance,created_at,updated_at FROM event_reaction_windows WHERE event_id=:event_id ORDER BY instrument_symbol,horizon LIMIT :limit"""
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
]
