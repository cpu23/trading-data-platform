"""Data quality check functions for the orchestrator.

Each check accepts a config dict and returns a result dict with at least
``healthy: bool`` and ``detail: str`` keys.  All database access flows
through ``db.get_session`` so callers (or tests) can supply a real or
mock ``config`` dict.
"""

import statistics
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
    max_age_hours: float,
    config: dict,
) -> dict:
    """Verify the most recent row for *source_id* is not too old.

    Returns
    -------
    dict
        ``healthy``, ``detail``, ``latest_at`` (ISO str | None),
        ``age_hours`` (float | None).
    """
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
            "detail": f"stale ({age_hours:.1f}h old, max {max_age_hours}h)",
            "latest_at": latest_at.isoformat(),
            "age_hours": round(age_hours, 2),
        }

    return {
        "healthy": True,
        "detail": "fresh",
        "latest_at": latest_at.isoformat(),
        "age_hours": round(age_hours, 2),
    }


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

    # Walk the 14-day window and find gaps.
    gaps = []
    expected = set()
    for i in range(15):  # inclusive of today
        expected.add(now - timedelta(days=i))

    missing = sorted(expected - present_dates)
    # Group consecutive missing days into gap descriptions.
    if missing:
        start = missing[0]
        prev = missing[0]
        for d in missing[1:]:
            if (prev - d).days == 1:
                prev = d
            else:
                days = (start - prev).days + 1
                gaps.append(f"{start.isoformat()} → {prev.isoformat()} ({days} day" + ("s" if days != 1 else "") + ")")
                start = d
                prev = d
        days = (start - prev).days + 1
        gaps.append(f"{start.isoformat()} → {prev.isoformat()} ({days} day" + ("s" if days != 1 else "") + ")")

    # Report all gaps found
    if gaps:
        logger.warning("check_gaps_unhealthy", source_id=source_id, gap_count=len(gaps))
        return {
            "healthy": False,
            "detail": f"gap(s) found: {len(gaps)} missing period(s)",
            "gaps": gaps,
        }

    return {
        "healthy": True,
        "detail": "no gaps",
        "gaps": [],
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
            "detail": f"{dupes} duplicate row(s) detected",
            "total_count": total,
            "distinct_count": distinct,
            "duplicate_count": dupes,
        }

    return {
        "healthy": True,
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
            "detail": f"{len(anomalies)} anomaly(s) detected",
            "anomalies": anomalies,
            "mean": round(mean, 4),
            "stdev": round(stdev, 4),
        }

    return {
        "healthy": True,
        "detail": "no anomalies",
        "anomalies": [],
        "mean": round(mean, 4),
        "stdev": round(stdev, 4),
    }


# ---------------------------------------------------------------------------
# Registered checks — callable dict for orchestration
# ---------------------------------------------------------------------------

DATA_QUALITY_CHECKS = {
    "fred_freshness": lambda config=None: check_freshness(
        source_id="fred",
        table="macro_series",
        timestamp_column="observed_at",
        max_age_hours=30,
        config=config or {},
    ),
    "fred_gaps": lambda config=None: check_gaps(
        source_id="fred",
        table="macro_series",
        date_column="observed_at",
        expected_interval="1 day",
        config=config or {},
    ),
    "fred_anomalies": lambda config=None: check_anomalies(
        source_id="fred",
        table="macro_series",
        value_column="value",
        timestamp_column="observed_at",
        config=config or {},
    ),
    "forex_factory_freshness": lambda config=None: check_freshness(
        source_id="forex_factory",
        table="econ_events",
        timestamp_column="scheduled_at",
        max_age_hours=14 * 24,  # 14 days
        config=config or {},
    ),
    "forex_factory_dupes": lambda config=None: check_duplicates(
        source_id="forex_factory",
        table="econ_events",
        unique_columns=["event_id"],
        config=config or {},
    ),
    "oanda_freshness": lambda config=None: check_freshness(
        source_id="oanda",
        table="market_data",
        timestamp_column="timestamp",
        max_age_hours=12,
        config=config or {},
    ),
}
