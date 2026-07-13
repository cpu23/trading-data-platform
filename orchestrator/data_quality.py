"""Data quality check functions for the orchestrator.

Each check accepts a config dict and returns a result dict with at least
``healthy: bool`` and ``detail: str`` keys.  All database access flows
through ``db.get_session`` so callers (or tests) can supply a real or
mock ``config`` dict.
"""

import statistics
from datetime import date, datetime, timedelta, timezone

from db import get_session
from logging_config import get_logger
from sqlalchemy import text

logger = get_logger("data_quality")


# ---------------------------------------------------------------------------
# Business-day helpers — simple weekday calendar (no holiday package)
# ---------------------------------------------------------------------------

def _is_business_day(d: date) -> bool:
    """Return True for Monday–Friday."""
    return d.weekday() < 5  # Mon=0 … Fri=4


def _count_business_days(start: date, end: date) -> int:
    """Count business days in [start, end] inclusive."""
    count = 0
    cur = start
    while cur <= end:
        if _is_business_day(cur):
            count += 1
        cur += timedelta(days=1)
    return count


def _resolve_grace_hours(
    config: dict,
    source_id: str,
    frequency: str | None,
    fallback_hours: float,
) -> float:
    """Resolve the effective max-age in **hours** from config.

    When *frequency* is given, looks up
    ``config.data_quality.<source_id>.grace_periods.<frequency>`` and
    treats the value as **days** (converted to hours).  Falls back to
    *fallback_hours* when config is missing or *frequency* is None.
    """
    if not frequency:
        return fallback_hours

    dq = config.get("data_quality", {})
    src_cfg = dq.get(source_id, {})
    grace = src_cfg.get("grace_periods", {})
    days = grace.get(frequency)
    if days is not None:
        return float(days) * 24.0
    return fallback_hours


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------

def check_freshness(
    source_id: str,
    table: str,
    timestamp_column: str,
    max_age_hours: float,
    config: dict,
    *,
    frequency: str | None = None,
    series_id: str | None = None,
) -> dict:
    """Verify the most recent row for *source_id* is not too old.

    Parameters
    ----------
    frequency : str or None
        One of ``"daily"``, ``"weekly"``, ``"monthly"``, ``"quarterly"``.
        When provided the grace period is read from *config* and business-day
        logic is applied for ``"daily"``.
    series_id : str or None
        If given, the SQL query is scoped to a single FRED series.
    """
    grace_hours = _resolve_grace_hours(
        config, source_id, frequency, max_age_hours,
    )
    logger.info(
        "check_freshness_running",
        source_id=source_id,
        table=table,
        max_age_hours=grace_hours,
        frequency=frequency,
        series_id=series_id,
    )

    # Build WHERE clause — always filter by source; optionally by series_id
    where = "WHERE source = :source_id"
    params: dict = {"source_id": source_id}
    if series_id:
        where += " AND series_id = :series_id"
        params["series_id"] = series_id

    sql = text(
        f"SELECT MAX({timestamp_column}) FROM {table} {where}"
    )
    with get_session(config) as session:
        result = session.execute(sql, params)
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

    # ── future timestamp → mark as "future" ────────────────────────────
    if latest_at > now:
        logger.warning(
            "check_freshness_future",
            source_id=source_id,
            latest_at=latest_at.isoformat(),
        )
        return {
            "healthy": False,
            "detail": f"future timestamp ({latest_at.isoformat()})",
            "latest_at": latest_at.isoformat(),
            "age_hours": 0.0,
        }

    # ── business-day age for daily-frequency series ────────────────────
    if frequency == "daily":
        # Count business days between latest date and today
        latest_date = latest_at.date()
        today = now.date()
        bdays = _count_business_days(latest_date, today) - 1  # same day → 0
        if bdays < 0:
            bdays = 0
        # Convert grace from hours back to business days for comparison
        grace_bdays = grace_hours / 24.0
        if bdays > grace_bdays:
            logger.warning(
                "check_freshness_unhealthy",
                source_id=source_id,
                business_days=bdays,
                grace_bdays=grace_bdays,
            )
            return {
                "healthy": False,
                "detail": (
                    f"stale ({bdays} business day(s) old, "
                    f"max {grace_bdays:.0f})"
                ),
                "latest_at": latest_at.isoformat(),
                "age_hours": round(float(bdays) * 24.0, 2),
            }
        return {
            "healthy": True,
            "detail": "fresh",
            "latest_at": latest_at.isoformat(),
            "age_hours": round(float(bdays) * 24.0, 2),
        }

    # ── default absolute-hours check ───────────────────────────────────
    age = now - latest_at
    age_hours = age.total_seconds() / 3600.0

    if age_hours > grace_hours:
        logger.warning(
            "check_freshness_unhealthy",
            source_id=source_id,
            age_hours=round(age_hours, 2),
            max_age_hours=grace_hours,
        )
        return {
            "healthy": False,
            "detail": f"stale ({age_hours:.1f}h old, max {grace_hours:.0f}h)",
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
    *,
    frequency: str | None = None,
    series_id: str | None = None,
) -> dict:
    """Detect missing days in a time-series.

    When *frequency* is ``"daily"`` weekends are excluded from the expected
    date set.  When *series_id* is provided the query is scoped to a single
    series.
    """
    logger.info(
        "check_gaps_running",
        source_id=source_id,
        table=table,
        frequency=frequency,
        series_id=series_id,
    )
    now = datetime.now(timezone.utc).date()
    cutoff = now - timedelta(days=14)

    where = "WHERE source = :source_id"
    params: dict = {"source_id": source_id, "cutoff": cutoff}
    if series_id:
        where += " AND series_id = :series_id"
        params["series_id"] = series_id

    sql = text(
        f"SELECT DISTINCT {date_column}::date FROM {table} "
        f"{where} "
        f"AND {date_column} >= :cutoff "
        f"ORDER BY {date_column}::date DESC"
    )
    with get_session(config) as session:
        result = session.execute(sql, params)
        rows = result.fetchall()

    # Build a set of dates that have data.
    present_dates: set[date] = set()
    for row in rows:
        d = row[0]
        if isinstance(d, str):
            d = datetime.fromisoformat(d).date()
        elif isinstance(d, datetime):
            d = d.date()
        present_dates.add(d)

    # Build the expected set — skip weekends for daily frequency
    expected: set[date] = set()
    for i in range(15):  # inclusive of today
        d = now - timedelta(days=i)
        if frequency == "daily" and not _is_business_day(d):
            continue
        expected.add(d)

    missing = sorted(expected - present_dates)
    # Group consecutive missing days into gap descriptions.
    gaps = []
    if missing:
        start = missing[0]
        prev = missing[0]
        for d in missing[1:]:
            if (prev - d).days == 1:
                prev = d
            else:
                days = (start - prev).days + 1
                gaps.append(
                    f"{start.isoformat()} → {prev.isoformat()} "
                    f"({days} day" + ("s" if days != 1 else "") + ")"
                )
                start = d
                prev = d
        days = (start - prev).days + 1
        gaps.append(
            f"{start.isoformat()} → {prev.isoformat()} "
            f"({days} day" + ("s" if days != 1 else "") + ")"
        )

    if gaps:
        logger.warning(
            "check_gaps_unhealthy",
            source_id=source_id,
            gap_count=len(gaps),
        )
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
    *,
    series_id: str | None = None,
) -> dict:
    """Flag value outliers using simple z-score on the last 30 days.

    When *series_id* is given the query is scoped so values from different
    series are never mixed in the same z-score calculation.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    logger.info(
        "check_anomalies_running",
        source_id=source_id,
        table=table,
        z_threshold=z_threshold,
        series_id=series_id,
    )

    where = "WHERE source = :source_id"
    params: dict = {"source_id": source_id, "cutoff": cutoff}
    if series_id:
        where += " AND series_id = :series_id"
        params["series_id"] = series_id

    sql = text(
        f"SELECT {value_column} FROM {table} "
        f"{where} "
        f"AND {timestamp_column} >= :cutoff "
        f"ORDER BY {timestamp_column} DESC"
    )
    with get_session(config) as session:
        result = session.execute(sql, params)
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
