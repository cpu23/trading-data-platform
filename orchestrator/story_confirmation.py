"""Deterministic headline-market confirmation observations.

The flags in this module describe observed price paths.  They are not trading
signals and do not infer an expected profitable direction.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import text

from contracts.db_results import result_first, result_rows

FLAGS = (
    "confirmed_by_market",
    "no_material_reaction",
    "market_moved_before_headline",
    "cross_asset_divergence",
    "initial_move_reversed",
    "insufficient_market_data",
)
_MAX_ROWS = 5000
_MAX_LIST = 500


def _timestamp(value: Any, default: datetime | None = None) -> datetime | None:
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


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None




def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _settings(config: Any) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    value = config.get("story_confirmation", {})
    return value if isinstance(value, Mapping) else {}


def _symbols(markets: Any) -> list[str]:
    if isinstance(markets, str):
        try:
            markets = json.loads(markets)
        except json.JSONDecodeError:
            markets = []
    if not isinstance(markets, Sequence):
        return []
    values: list[str] = []
    for item in markets[:100]:
        if isinstance(item, Mapping):
            value = item.get("symbol") or item.get("canonical_id")
        else:
            value = item
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in values:
            values.append(symbol[:32])
    return values


def session_target(headline_at: datetime, settings: Mapping[str, Any]) -> datetime:
    close = time(21, 0)
    raw = settings.get("session_close", "21:00:00")
    if isinstance(raw, str):
        try:
            close = time.fromisoformat(raw)
        except ValueError:
            pass
    target = datetime.combine(headline_at.date(), close, tzinfo=UTC)
    return target if target > headline_at else target + timedelta(days=1)


def _nearest(
    rows: Sequence[Mapping[str, Any]],
    target: datetime,
    tolerance: timedelta,
    *,
    before_only: bool = False,
) -> tuple[datetime, float] | None:
    choices = []
    for row in rows:
        stamp, price = _timestamp(row.get("timestamp")), _finite(row.get("close"))
        if stamp is None or price is None or price <= 0:
            continue
        if before_only and stamp > target:
            continue
        distance = abs((stamp - target).total_seconds())
        if distance <= tolerance.total_seconds():
            choices.append((distance, stamp, price))
    if not choices:
        return None
    _, stamp, price = min(choices, key=lambda item: (item[0], item[1]))
    return stamp, price


def _move(
    baseline: tuple[datetime, float] | None, target: tuple[datetime, float] | None
) -> float | None:
    if baseline is None or target is None or baseline[1] == 0:
        return None
    result = (target[1] / baseline[1] - 1.0) * 100.0
    return result if math.isfinite(result) else None


def _significant(value: float | None, threshold_percent: float) -> bool:
    return value is not None and abs(value) >= threshold_percent


def _global_flags(
    observations: Sequence[Mapping[str, Any]], threshold_percent: float
) -> list[str]:
    post = [
        value
        for row in observations
        for value in (row.get("move_5m"), row.get("move_30m"), row.get("move_session"))
        if _finite(value) is not None
    ]
    significant = [
        float(value)
        for value in post
        if _significant(_finite(value), threshold_percent)
    ]
    flags: list[str] = []
    if significant:
        flags.append("confirmed_by_market")
    elif post:
        flags.append("no_material_reaction")
    if any(
        _significant(_finite(row.get("pre_headline_move")), threshold_percent)
        for row in observations
    ):
        flags.append("market_moved_before_headline")
    for key in ("move_5m", "move_30m", "move_session"):
        values = [
            _finite(row.get(key))
            for row in observations
            if _significant(_finite(row.get(key)), threshold_percent)
        ]
        if any(value > 0 for value in values if value is not None) and any(
            value < 0 for value in values if value is not None
        ):
            flags.append("cross_asset_divergence")
            break
    if any(
        _significant(_finite(row.get("move_5m")), threshold_percent)
        and any(
            _significant(_finite(row.get(key)), threshold_percent)
            and _finite(row.get("move_5m")) * _finite(row.get(key)) < 0
            for key in ("move_30m", "move_session")
        )
        for row in observations
    ):
        flags.append("initial_move_reversed")
    if not post or any(row.get("missing_reasons") for row in observations):
        flags.append("insufficient_market_data")
    return [name for name in FLAGS if name in flags]


def calculate_story_confirmation(
    session: Any,
    cluster_id: Any,
    event_id: Any,
    config: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Calculate and upsert bounded observations for one story event."""
    settings = _settings(config)
    current = _timestamp(now, datetime.now(UTC)) or datetime.now(UTC)
    context = result_first(session.execute(
        text("""SELECT c.id AS cluster_id, c.markets, e.id AS event_id,
            COALESCE(e.published_at, e.effective_at, e.observed_at) AS headline_at
            FROM story_clusters c JOIN market_events e ON e.id = :event_id
            WHERE c.id = :cluster_id LIMIT 1"""),
        {"cluster_id": cluster_id, "event_id": event_id},
    ))
    if context is None:
        return {
            "cluster_id": str(cluster_id),
            "event_id": str(event_id),
            "updated": 0,
            "flags": ["insufficient_market_data"],
        }
    headline_at = _timestamp(context.get("headline_at"))
    symbols = _symbols(context.get("markets"))
    if headline_at is None or not symbols:
        return {
            "cluster_id": str(cluster_id),
            "event_id": str(event_id),
            "updated": 0,
            "flags": ["insufficient_market_data"],
        }
    try:
        tolerance = timedelta(
            minutes=max(1, min(30, int(settings.get("target_tolerance_minutes", 5))))
        )
        pre_minutes = max(1, min(60, int(settings.get("pre_headline_minutes", 5))))
        row_limit = max(1, min(_MAX_ROWS, int(settings.get("query_limit", 5000))))
        threshold_percent = max(
            0.0, min(100.0, float(settings.get("material_move_percent", 0.25)))
        )
    except (TypeError, ValueError, OverflowError):
        tolerance, pre_minutes, row_limit, threshold_percent = (
            timedelta(minutes=5),
            5,
            5000,
            0.25,
        )
    session_close = session_target(headline_at, settings)
    rows = result_rows(session.execute(
        text("""SELECT symbol, timestamp, close FROM market_data
            WHERE symbol = ANY(:symbols) AND timeframe = 'PRICE'
              AND timestamp >= :start_at AND timestamp <= :end_at
            ORDER BY timestamp ASC LIMIT :row_limit"""),
        {
            "symbols": symbols,
            "start_at": headline_at - timedelta(minutes=pre_minutes) - tolerance,
            "end_at": min(current, session_close + tolerance),
            "row_limit": row_limit,
        },
    ))
    by_symbol = {
        symbol: [row for row in rows if str(row.get("symbol", "")).upper() == symbol]
        for symbol in symbols
    }
    observations: list[dict[str, Any]] = []
    for symbol in symbols:
        source = by_symbol[symbol]
        baseline = _nearest(source, headline_at, tolerance, before_only=True)
        pre = _nearest(source, headline_at - timedelta(minutes=pre_minutes), tolerance)
        targets = {
            "move_5m": _nearest(source, headline_at + timedelta(minutes=5), tolerance)
            if current >= headline_at + timedelta(minutes=5)
            else None,
            "move_30m": _nearest(source, headline_at + timedelta(minutes=30), tolerance)
            if current >= headline_at + timedelta(minutes=30)
            else None,
            "move_session": _nearest(source, session_close, tolerance)
            if current >= session_close
            else None,
        }
        missing: dict[str, str] = {}
        if baseline is None:
            missing["baseline"] = "missing_price_near_headline"
        values = {
            "pre_headline_move": _move(pre, baseline),
            **{key: _move(baseline, target) for key, target in targets.items()},
        }
        if pre is None:
            missing["pre_headline_move"] = "missing_pre_headline_price"
        for key, target in targets.items():
            due_at = (
                headline_at + timedelta(minutes=5 if key == "move_5m" else 30)
                if key != "move_session"
                else session_close
            )
            if current < due_at:
                missing[key] = "not_due"
            elif target is None:
                missing[key] = "missing_target_price"
        observations.append(
            {
                "symbol": symbol,
                **values,
                "missing_reasons": missing,
                "baseline_at": baseline[0].isoformat() if baseline else None,
            }
        )
    flags = _global_flags(observations, threshold_percent)
    updated = 0
    for observation in observations:
        session.execute(
            text("""INSERT INTO story_market_confirmations
                (cluster_id, source_event_id, market_symbol, headline_at, observed_at,
                 pre_headline_move, move_5m, move_30m, move_session, flags,
                 missing_reasons, provenance)
                VALUES (:cluster_id, :event_id, :symbol, :headline_at, :observed_at,
                 :pre_move, :move_5m, :move_30m, :move_session, CAST(:flags AS JSONB),
                 CAST(:missing AS JSONB), CAST(:provenance AS JSONB))
                ON CONFLICT (cluster_id, source_event_id, market_symbol) DO UPDATE SET
                 observed_at = EXCLUDED.observed_at,
                 pre_headline_move = EXCLUDED.pre_headline_move,
                 move_5m = EXCLUDED.move_5m, move_30m = EXCLUDED.move_30m,
                 move_session = EXCLUDED.move_session, flags = EXCLUDED.flags,
                 missing_reasons = EXCLUDED.missing_reasons,
                 provenance = EXCLUDED.provenance, updated_at = NOW()"""),
            {
                "cluster_id": cluster_id,
                "event_id": event_id,
                "symbol": observation["symbol"],
                "headline_at": headline_at,
                "observed_at": current,
                "pre_move": observation["pre_headline_move"],
                "move_5m": observation["move_5m"],
                "move_30m": observation["move_30m"],
                "move_session": observation["move_session"],
                "flags": _json(flags),
                "missing": _json(observation["missing_reasons"]),
                "provenance": _json(
                    {
                        "source_table": "market_data",
                        "timeframe": "PRICE",
                        "target_tolerance_seconds": int(tolerance.total_seconds()),
                        "material_move_percent": threshold_percent,
                        "row_limit": row_limit,
                        "baseline_at": observation["baseline_at"],
                    }
                ),
            },
        )
        updated += 1
    return {
        "cluster_id": str(cluster_id),
        "event_id": str(event_id),
        "updated": updated,
        "flags": flags,
        "observations": observations,
    }


def list_story_confirmations(
    session: Any, cluster_id: Any, limit: int = 100
) -> list[dict[str, Any]]:
    bounded = max(1, min(_MAX_LIST, int(limit)))
    return result_rows(session.execute(
        text("""SELECT id, cluster_id, source_event_id, market_symbol, headline_at,
            observed_at, pre_headline_move, move_5m, move_30m, move_session,
            flags, missing_reasons, provenance, created_at, updated_at
            FROM story_market_confirmations WHERE cluster_id = :cluster_id
            ORDER BY observed_at DESC, market_symbol LIMIT :limit"""),
        {"cluster_id": cluster_id, "limit": bounded},
    ))


def backfill_story_confirmations(
    session: Any, config: Any = None, now: Any = None, limit: int = 100
) -> dict[str, int]:
    current = _timestamp(now, datetime.now(UTC)) or datetime.now(UTC)
    bounded = max(1, min(500, int(limit)))
    rows = result_rows(session.execute(
        text("""SELECT DISTINCT cluster_id, source_event_id
            FROM story_market_confirmations
            WHERE flags ? 'insufficient_market_data' AND headline_at <= :now
            ORDER BY cluster_id, source_event_id LIMIT :limit"""),
        {"now": current, "limit": bounded},
    ))
    updated = 0
    for row in rows:
        result = calculate_story_confirmation(
            session, row["cluster_id"], row["source_event_id"], config, current
        )
        updated += int(result.get("updated", 0))
    return {"scanned": len(rows), "updated": updated}


__all__ = [
    "FLAGS",
    "backfill_story_confirmations",
    "calculate_story_confirmation",
    "list_story_confirmations",
    "session_target",
]
