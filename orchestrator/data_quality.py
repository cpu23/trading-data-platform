"""Data quality check functions for the orchestrator.

Each check accepts a config dict and returns a result dict with at least
``healthy: bool`` and ``detail: str`` keys.  All database access flows
through ``db.get_session`` so callers (or tests) can supply a real or
mock ``config`` dict.
"""

import statistics
import re
from datetime import datetime, timedelta, timezone

from db import get_session
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("data_quality")


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------

def check_freshness(
    source_id: str,
    table: str,
    timestamp_column: str,
    max_age_hours: float | None,
    config: dict,
) -> dict:
    """Verify the most recent row for *source_id* is not too old.

    Returns
    -------
    dict
        ``healthy``, ``detail``, ``latest_at`` (ISO str | None),
        ``age_hours`` (float | None).
    """
    max_age_hours = max_age_hours or _freshness_sla_hours(source_id, config)
    logger.info("check_freshness_running", source_id=source_id, table=table, max_age_hours=max_age_hours)
    sql = text(
        f"SELECT MAX({timestamp_column}) FROM {table} WHERE source = :source_id"
    )
    with get_session(config) as session:
        result = session.execute(sql, {"source_id": source_id})
        row = result.fetchone()

    if row is None or row[0] is None:
        return {
            "healthy": False,
            "state": "no_data",
            "source_id": source_id,
            "detail": "no data",
            "latest_at": None,
            "age_hours": None,
        }

    latest_at = row[0]
    if isinstance(latest_at, str):
        latest_at = datetime.fromisoformat(latest_at.replace("Z", "+00:00"))

    now = datetime.now(timezone.utc)
    # Ensure both are offset-aware for subtraction
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=timezone.utc)

    age = now - latest_at
    age_hours = age.total_seconds() / 3600.0

    if age_hours > max_age_hours:
        logger.warning("check_freshness_unhealthy", source_id=source_id, age_hours=round(age_hours, 2), max_age_hours=max_age_hours)
        return {
            "healthy": False,
            "state": "stale",
            "source_id": source_id,
            "detail": f"stale ({age_hours:.1f}h old, max {max_age_hours}h)",
            "latest_at": latest_at.isoformat(),
            "age_hours": round(age_hours, 2),
        }

    return {
        "healthy": True,
        "state": "healthy",
        "source_id": source_id,
        "detail": "fresh",
        "latest_at": latest_at.isoformat(),
        "age_hours": round(age_hours, 2),
    }


def _freshness_sla_hours(source_id: str, config: dict) -> float:
    source = config.get("collectors", {}).get(source_id, {})
    if source.get("freshness_hours"):
        return float(source["freshness_hours"])
    schedule = source.get("schedule", "")
    # Conservative schedule-aware defaults: weekly jobs receive eight days,
    # daily jobs 36 hours, and intraday jobs three expected intervals.
    if re.match(r"^\S+\s+\S+\s+\S+\s+\S+\s+[0-6]$", schedule):
        return 8 * 24
    if schedule:
        hour_field = schedule.split()[1] if len(schedule.split()) >= 2 else ""
        if "/" in hour_field:
            try:
                interval = int(hour_field.rsplit("/", 1)[1])
                return max(3.0, interval * 3.0)
            except ValueError:
                pass
        return 36.0
    return 36.0


def check_source_freshness(
    source_id: str,
    table: str,
    timestamp_column: str,
    config: dict,
) -> dict:
    source_config = config.get("collectors", {}).get(source_id, {})
    if not source_config.get("enabled", True):
        return {
            "healthy": True,
            "state": "disabled",
            "source_id": source_id,
            "detail": "disabled",
            "latest_at": None,
            "age_hours": None,
        }
    return check_freshness(
        source_id=source_id,
        table=table,
        timestamp_column=timestamp_column,
        max_age_hours=None,
        config=config,
    )


def check_gaps(
    source_id: str,
    table: str,
    date_column: str,
    expected_interval: str,
    config: dict,
    max_gap_days: int = 3,
) -> dict:
    """Detect missing days in a daily time-series.

    Queries distinct dates for the last 14 days and reports gaps larger
    than *expected_interval* (e.g. ``"1 day"``).

    Returns
    -------
    dict
        ``healthy``, ``detail``, ``gaps`` (list of gap descriptions).
    """
    logger.info("check_gaps_running", source_id=source_id, table=table)
    now = datetime.now(timezone.utc).date()
    cutoff = now - timedelta(days=14)

    sql = text(
        f"SELECT DISTINCT {date_column}::date FROM {table} "
        f"WHERE source = :source_id AND {date_column} >= :cutoff "
        f"ORDER BY {date_column}::date DESC"
    )
    with get_session(config) as session:
        result = session.execute(sql, {"source_id": source_id, "cutoff": cutoff})
        rows = result.fetchall()

    # Build a set of dates that have data.
    present_dates = set()
    for row in rows:
        d = row[0]
        if isinstance(d, str):
            d = datetime.fromisoformat(d).date()
        elif isinstance(d, datetime):
            d = d.date()
        present_dates.add(d)

    # Walk the window according to the declared frequency. Daily series skip
    # weekends; weekly/monthly series compare at their natural cadence.
    gaps = []
    expected = set()
    interval = expected_interval.strip().lower()
    if "week" in interval:
        expected = {now - timedelta(days=7 * i) for i in range(3)}
    elif "month" in interval:
        expected = {
            (now.replace(day=1) - timedelta(days=32 * i)).replace(day=1)
            for i in range(3)
        }
    else:
        expected = {
            now - timedelta(days=i)
            for i in range(15)
            if (now - timedelta(days=i)).weekday() < 5
        }

    missing = sorted(expected - present_dates)
    # Group consecutive missing days into gap descriptions.
    if missing:
        start = missing[0]
        prev = missing[0]
        for d in missing[1:]:
            if (d - prev).days <= max_gap_days:
                prev = d
            else:
                days = (prev - start).days + 1
                gaps.append(f"{start.isoformat()} → {prev.isoformat()} ({days} day" + ("s" if days != 1 else "") + ")")
                start = d
                prev = d
        days = (prev - start).days + 1
        gaps.append(f"{start.isoformat()} → {prev.isoformat()} ({days} day" + ("s" if days != 1 else "") + ")")

    # Report all gaps found
    if gaps:
        logger.warning("check_gaps_unhealthy", source_id=source_id, gap_count=len(gaps))
        return {
            "healthy": False,
            "state": "gaps",
            "source_id": source_id,
            "detail": f"gap(s) found: {len(gaps)} missing period(s)",
            "gaps": gaps,
        }

    return {
        "healthy": True,
        "state": "healthy",
        "source_id": source_id,
        "detail": "no gaps",
        "gaps": [],
    }


def check_macro_series_gaps(source_id: str, config: dict) -> dict:
    """Check each macro series at its declared frequency."""
    if not config.get("collectors", {}).get(source_id, {}).get("enabled", True):
        return {
            "healthy": True,
            "state": "disabled",
            "source_id": source_id,
            "detail": "disabled",
            "gaps": [],
        }
    cutoff = datetime.now(timezone.utc) - timedelta(days=730)
    sql = text(
        "SELECT series_id, observed_at::date, "
        "COALESCE(metadata->>'frequency', 'daily') "
        "FROM macro_series WHERE source = :source_id "
        "AND observed_at >= :cutoff ORDER BY series_id, observed_at"
    )
    with get_session(config) as session:
        rows = session.execute(
            sql, {"source_id": source_id, "cutoff": cutoff}
        ).fetchall()

    by_series: dict[str, dict] = {}
    for series_id, observed_at, frequency in rows:
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at).date()
        elif isinstance(observed_at, datetime):
            observed_at = observed_at.date()
        entry = by_series.setdefault(
            series_id, {"frequency": str(frequency).lower(), "dates": []}
        )
        entry["dates"].append(observed_at)

    allowed_days = {
        "daily": 4,
        "weekly": 14,
        "monthly": 62,
        "quarterly": 185,
        "annual": 550,
    }
    gaps = []
    for series_id, entry in by_series.items():
        dates = sorted(set(entry["dates"]))
        threshold = allowed_days.get(entry["frequency"], 62)
        for previous, current in zip(dates, dates[1:]):
            interval_days = (current - previous).days
            if interval_days > threshold:
                gaps.append(
                    {
                        "series_id": series_id,
                        "frequency": entry["frequency"],
                        "from": previous.isoformat(),
                        "to": current.isoformat(),
                        "days": interval_days,
                    }
                )

    if not rows:
        return {
            "healthy": False,
            "state": "no_data",
            "source_id": source_id,
            "detail": "no data",
            "gaps": [],
        }
    return {
        "healthy": not gaps,
        "state": "healthy" if not gaps else "gaps",
        "source_id": source_id,
        "detail": "no gaps" if not gaps else f"{len(gaps)} frequency-aware gap(s)",
        "gaps": gaps,
    }


def check_duplicates(
    source_id: str,
    table: str,
    unique_columns: list[str],
    config: dict,
) -> dict:
    """Count total rows vs distinct rows on *unique_columns*.

    Returns
    -------
    dict
        ``healthy``, ``detail``, ``total_count``, ``distinct_count``,
        ``duplicate_count``.
    """
    distinct_expr = ", ".join(unique_columns)
    logger.info("check_duplicates_running", source_id=source_id, table=table)
    sql = text(
        f"SELECT COUNT(*) AS total, COUNT(DISTINCT ({distinct_expr})) AS distinct_count "
        f"FROM {table} WHERE source = :source_id"
    )
    with get_session(config) as session:
        result = session.execute(sql, {"source_id": source_id})
        row = result.fetchone()

    if row is None:
        return {
            "healthy": True,
            "state": "no_data",
            "source_id": source_id,
            "detail": "no data",
            "total_count": 0,
            "distinct_count": 0,
            "duplicate_count": 0,
        }

    total = int(row[0] or 0)
    distinct = int(row[1] or 0)
    dupes = total - distinct

    if dupes > 0:
        logger.warning("check_duplicates_unhealthy", source_id=source_id, duplicate_count=dupes)
        return {
            "healthy": False,
            "state": "duplicates",
            "source_id": source_id,
            "detail": f"{dupes} duplicate row(s) detected",
            "total_count": total,
            "distinct_count": distinct,
            "duplicate_count": dupes,
        }

    return {
        "healthy": True,
        "state": "healthy",
        "source_id": source_id,
        "detail": "no duplicates",
        "total_count": total,
        "distinct_count": distinct,
        "duplicate_count": 0,
    }


def check_anomalies(
    source_id: str,
    table: str,
    value_column: str,
    timestamp_column: str,
    config: dict,
    z_threshold: float = 5.0,
) -> dict:
    """Flag value outliers using simple z-score on the last 30 days.

    Computes mean and stdev over the full 30-day window, then checks
    the most-recent 5 values against *z_threshold*.

    Returns
    -------
    dict
        ``healthy``, ``detail``, ``anomalies`` (list of descriptions),
        ``mean``, ``stdev``.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    logger.info("check_anomalies_running", source_id=source_id, table=table, z_threshold=z_threshold)

    sql = text(
        f"SELECT {value_column} FROM {table} "
        f"WHERE source = :source_id AND {timestamp_column} >= :cutoff "
        f"ORDER BY {timestamp_column} DESC"
    )
    with get_session(config) as session:
        result = session.execute(sql, {"source_id": source_id, "cutoff": cutoff})
        rows = result.fetchall()

    values = [float(row[0]) for row in rows if row[0] is not None]

    if len(values) < 5:
        return {
            "healthy": True,
            "state": "insufficient_data",
            "source_id": source_id,
            "detail": f"insufficient data ({len(values)} values, need >=5)",
            "anomalies": [],
            "mean": None,
            "stdev": None,
        }

    mean = statistics.mean(values)
    stdev = statistics.stdev(values) if len(values) >= 2 else 0.0

    if stdev == 0.0:
        return {
            "healthy": True,
            "state": "healthy",
            "source_id": source_id,
            "detail": "no variance — all values equal",
            "anomalies": [],
            "mean": round(mean, 4),
            "stdev": 0.0,
        }

    # Check the 5 most recent values for outliers.
    recent = values[:5]
    anomalies = []
    for i, val in enumerate(recent):
        z = abs(val - mean) / stdev
        if z > z_threshold:
            anomalies.append(
                f"value {val} at position {i+1} (z={z:.2f}, threshold={z_threshold})"
            )

    if anomalies:
        logger.warning("check_anomalies_unhealthy", source_id=source_id, anomaly_count=len(anomalies))
        return {
            "healthy": False,
            "state": "anomalies",
            "source_id": source_id,
            "detail": f"{len(anomalies)} anomaly(s) detected",
            "anomalies": anomalies,
            "mean": round(mean, 4),
            "stdev": round(stdev, 4),
        }

    return {
        "healthy": True,
        "state": "healthy",
        "source_id": source_id,
        "detail": "no anomalies",
        "anomalies": [],
        "mean": round(mean, 4),
        "stdev": round(stdev, 4),
    }


# ---------------------------------------------------------------------------
# Registered checks — callable dict for orchestration
# ---------------------------------------------------------------------------

DATA_QUALITY_CHECKS = {
    "fred_freshness": lambda config=None: check_source_freshness(
        source_id="fred",
        table="macro_series",
        timestamp_column="acquired_at",
        config=config or {},
    ),
    "fred_gaps": lambda config=None: check_macro_series_gaps(
        source_id="fred",
        config=config or {},
    ),
    "fred_anomalies": lambda config=None: check_anomalies(
        source_id="fred",
        table="macro_series",
        value_column="value",
        timestamp_column="observed_at",
        config=config or {},
    ),
    "forex_factory_freshness": lambda config=None: check_source_freshness(
        source_id="forex_factory",
        table="econ_events",
        timestamp_column="acquired_at",
        config=config or {},
    ),
    "forex_factory_dupes": lambda config=None: check_duplicates(
        source_id="forex_factory",
        table="econ_events",
        unique_columns=["event_id"],
        config=config or {},
    ),
    **{
        f"{source_id}_freshness": (
            lambda config=None, source_id=source_id, table=table: check_source_freshness(
                source_id=source_id,
                table=table,
                timestamp_column="acquired_at",
                config=config or {},
            )
        )
        for source_id, table in {
            "cftc": "positioning_reports",
            "central_banks": "source_documents",
            "oecd": "macro_series",
            "ecb": "macro_series",
            "boe": "macro_series",
            "eia": "macro_series",
        }.items()
    },
}
