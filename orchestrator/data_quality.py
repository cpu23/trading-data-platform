"""Data quality check functions for the orchestrator.

Each check accepts a config dict and returns a result dict with at least
``healthy: bool`` and ``detail: str`` keys.  All database access flows
through ``db.get_session`` so callers (or tests) can supply a real or
mock ``config`` dict.
"""

import re
import statistics
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from db import get_session
from logging_config import get_logger

logger = get_logger("data_quality")


_URL_CREDENTIALS = re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)")
_NAMED_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret)(\s*[=:]\s*)([^\s,;]+)"
)


def _safe_error_message(exc: Exception) -> str:
    """Return a bounded exception message with common credential forms redacted."""
    message = " ".join(str(exc).split()) or "quality check failed"
    message = _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", message)
    message = _NAMED_SECRET.sub(r"\1\2[REDACTED]", message)
    return message[:500]


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
    grace_key = "daily_business" if frequency == "daily" else frequency
    days = grace.get(grace_key)
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
    future_is_valid: bool = False,
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
        config,
        source_id,
        frequency,
        max_age_hours,
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

    sql = text(f"SELECT MAX({timestamp_column}) FROM {table} {where}")
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

    now = datetime.now(UTC)
    # Ensure both are offset-aware for subtraction
    if latest_at.tzinfo is None:
        latest_at = latest_at.replace(tzinfo=UTC)

    # ── future timestamp → mark as "future" ────────────────────────────
    if latest_at > now:
        logger.warning(
            "check_freshness_future",
            source_id=source_id,
            latest_at=latest_at.isoformat(),
        )
        return {
            "healthy": future_is_valid,
            "detail": f"future timestamp ({latest_at.isoformat()})",
            "freshness": "future",
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
                    f"stale ({bdays} business day(s) old, max {grace_bdays:.0f})"
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
    """Detect missing periods without treating lower-frequency data as daily.

    Daily series are checked through the current business day. Weekly, monthly,
    and quarterly series are checked for internal holes between the first and
    last observed period in a bounded history window; trailing publication lag
    remains the responsibility of :func:`check_freshness`.
    """
    logger.info(
        "check_gaps_running",
        source_id=source_id,
        table=table,
        frequency=frequency,
        series_id=series_id,
    )
    now = datetime.now(UTC).date()
    lookback_days = {
        "daily": 14,
        "weekly": 7 * 16,
        "monthly": 31 * 16,
        "quarterly": 93 * 16,
    }.get(frequency, 14)
    cutoff = now - timedelta(days=lookback_days)

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

    present_dates: set[date] = set()
    for row in rows:
        observed = row[0]
        if isinstance(observed, str):
            observed = datetime.fromisoformat(observed).date()
        elif isinstance(observed, datetime):
            observed = observed.date()
        present_dates.add(observed)

    if not present_dates:
        return {
            "healthy": True,
            "detail": "no data in bounded gap window",
            "gaps": [],
        }

    gaps: list[str]
    if frequency == "daily":
        expected = {
            now - timedelta(days=index)
            for index in range(15)
            if _is_business_day(now - timedelta(days=index))
        }
        gaps = [missing.isoformat() for missing in sorted(expected - present_dates)]
    elif frequency == "weekly":
        present_periods = {
            observed - timedelta(days=observed.weekday())
            for observed in present_dates
        }
        cursor = min(present_periods)
        end = max(present_periods)
        expected_periods: set[date] = set()
        while cursor <= end:
            expected_periods.add(cursor)
            cursor += timedelta(days=7)
        gaps = [
            f"week of {missing.isoformat()}"
            for missing in sorted(expected_periods - present_periods)
        ]
    elif frequency == "monthly":
        present_periods = {(observed.year, observed.month) for observed in present_dates}
        cursor_year, cursor_month = min(present_periods)
        end = max(present_periods)
        expected_periods: set[tuple[int, int]] = set()
        while (cursor_year, cursor_month) <= end:
            expected_periods.add((cursor_year, cursor_month))
            if cursor_month == 12:
                cursor_year += 1
                cursor_month = 1
            else:
                cursor_month += 1
        gaps = [
            f"{year:04d}-{month:02d}"
            for year, month in sorted(expected_periods - present_periods)
        ]
    elif frequency == "quarterly":
        present_periods = {
            (observed.year, ((observed.month - 1) // 3) + 1)
            for observed in present_dates
        }
        cursor_year, cursor_quarter = min(present_periods)
        end = max(present_periods)
        expected_periods: set[tuple[int, int]] = set()
        while (cursor_year, cursor_quarter) <= end:
            expected_periods.add((cursor_year, cursor_quarter))
            if cursor_quarter == 4:
                cursor_year += 1
                cursor_quarter = 1
            else:
                cursor_quarter += 1
        gaps = [
            f"{year:04d}-Q{quarter}"
            for year, quarter in sorted(expected_periods - present_periods)
        ]
    else:
        expected = {now - timedelta(days=index) for index in range(15)}
        gaps = [missing.isoformat() for missing in sorted(expected - present_dates)]

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
        logger.warning(
            "check_duplicates_unhealthy", source_id=source_id, duplicate_count=dupes
        )
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
    now = datetime.now(UTC)
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
                f"value {val} at position {i + 1} (z={z:.2f}, threshold={z_threshold})"
            )

    if anomalies:
        logger.warning(
            "check_anomalies_unhealthy",
            source_id=source_id,
            anomaly_count=len(anomalies),
        )
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
        future_is_valid=True,
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
        frequency="daily",
    ),
}


def _run_quality_check(check_id: str, check_fn, **metadata) -> dict:
    """Run one check, converting an operational exception into an unhealthy result."""
    try:
        result = check_fn()
    except Exception as exc:
        error_type = type(exc).__name__
        error_message = _safe_error_message(exc)
        logger.error(
            "quality_check_failed",
            check_id=check_id,
            error_type=error_type,
            error_message=error_message,
            **metadata,
        )
        return {
            "healthy": False,
            "detail": f"{error_type}: {error_message}",
            "error_type": error_type,
            "error_message": error_message,
            **metadata,
        }
    return {**result, **metadata}


def _source_enabled(config: dict, source_id: str) -> bool:
    """A source counts as enabled unless its collector explicitly says no."""
    collectors = config.get("collectors", {}) if isinstance(config, Mapping) else {}
    source_cfg = (
        collectors.get(source_id, {}) if isinstance(collectors, Mapping) else {}
    )
    if isinstance(source_cfg, Mapping):
        return bool(source_cfg.get("enabled", True))
    return True


def required_quality_checks(config: dict) -> set[str]:
    """Check ids that MUST be present for quality to be considered healthy.

    Checks for disabled sources are optional; FRED freshness/gaps/anomalies
    are required per configured series (falling back to the fixed FRED checks
    when no series are configured).  A missing required check degrades the
    overall quality verdict — empty/missing required checks are never healthy.
    """
    required: set[str] = set()
    collectors = config.get("collectors", {}) if isinstance(config, Mapping) else {}
    fred_series: list = []
    if isinstance(collectors, Mapping):
        fred_cfg = collectors.get("fred", {})
        if isinstance(fred_cfg, Mapping):
            fred_series = fred_cfg.get("series", []) or []

    for check_id in DATA_QUALITY_CHECKS:
        source_id = check_id.rsplit("_", 1)[0]
        if source_id == "fred":
            continue  # replaced by per-series checks below
        if _source_enabled(config, source_id):
            required.add(check_id)

    if _source_enabled(config, "fred"):
        if fred_series:
            for series in fred_series:
                if not isinstance(series, Mapping) or not series.get("id"):
                    continue
                series_id = str(series["id"])
                required.update(
                    {
                        f"fred_{series_id}_freshness",
                        f"fred_{series_id}_gaps",
                        f"fred_{series_id}_anomalies",
                    }
                )
        else:
            required.update({"fred_freshness", "fred_gaps", "fred_anomalies"})
    return required


def evaluate_quality(results: dict[str, dict], required: set[str]) -> str:
    """Map quality results to an overall verdict.

    ``"healthy"`` only when every required check is present and reports
    ``healthy is True`` exactly.  A required check that is missing or
    malformed (no boolean ``healthy`` key) yields ``"unknown"``; a required
    check that explicitly reports ``healthy is False`` yields
    ``"degraded"``.  With no required checks the verdict is ``"unknown"``
    when nothing was measured, otherwise ``"degraded"`` (optional failures
    are visible but never healthy).
    """
    if not required:
        return "unknown" if not results else "degraded"
    missing = required - set(results)
    if missing:
        return "unknown"
    verdicts: list[bool | None] = []
    for check_id in required:
        result = results.get(check_id)
        verdicts.append(result.get("healthy") if isinstance(result, dict) else None)
    if any(healthy is False for healthy in verdicts):
        return "degraded"
    if any(healthy is not True for healthy in verdicts):
        return "unknown"
    return "healthy"


def readiness_critical_checks(config: dict, required: set[str]) -> set[str]:
    """Checks that gate process readiness when failing.

    Only checks explicitly listed under ``readiness.data_quality_checks``
    (exact check ids, or a source id such as ``fred`` matching every check of
    that source) are readiness-critical; everything else is visible in the
    verdict but never blocks readiness, so a fresh install does not deadlock
    on a not-yet-run schedule.
    """
    readiness = config.get("readiness", {}) if isinstance(config, Mapping) else {}
    configured = readiness.get("data_quality_checks", []) if isinstance(readiness, Mapping) else []
    if not configured:
        return set()
    return {
        check_id
        for check_id in required
        if any(check_id == entry or check_id.startswith(f"{entry}_") for entry in configured)
    }


def normalize_quality_results(results: dict[str, dict]) -> dict[str, dict]:
    """Contract-valid check payloads; malformed entries become explicit failures.

    A malformed result (missing or non-boolean ``healthy``) must never reach
    the ``QualityCheck`` response contract — it is normalized to
    ``healthy: false, status: unknown`` while the overall verdict (computed
    from the raw results) stays ``unknown``.
    """
    normalized: dict[str, dict] = {}
    for check_id, result in results.items():
        if isinstance(result, dict) and result.get("healthy") in (True, False):
            normalized[check_id] = result
        else:
            normalized[check_id] = {
                "healthy": False,
                "status": "unhealthy",
                "detail": "malformed result",
            }
    return normalized


def run_quality_checks(config: dict) -> dict[str, dict]:
    """Run production checks, isolating failures and expanding FRED per series.

    Checks whose source is disabled in the configuration are skipped so a
    deliberately disabled source never degrades overall quality.
    """
    results: dict[str, dict] = {}
    series_config = config.get("collectors", {}).get("fred", {}).get("series", [])
    if _source_enabled(config, "fred"):
        for series in series_config:
            series_id = series["id"]
            frequency = series.get("frequency")
            checks = {
                "freshness": lambda series_id=series_id,
                frequency=frequency: check_freshness(
                    source_id="fred",
                    table="macro_series",
                    timestamp_column="observed_at",
                    max_age_hours=30,
                    config=config,
                    series_id=series_id,
                    frequency=frequency,
                ),
                "gaps": lambda series_id=series_id, frequency=frequency: check_gaps(
                    source_id="fred",
                    table="macro_series",
                    date_column="observed_at",
                    expected_interval="1 day",
                    config=config,
                    series_id=series_id,
                    frequency=frequency,
                ),
                "anomalies": lambda series_id=series_id: check_anomalies(
                    source_id="fred",
                    table="macro_series",
                    value_column="value",
                    timestamp_column="observed_at",
                    config=config,
                    series_id=series_id,
                ),
            }
            for check_name, check_fn in checks.items():
                check_id = f"fred_{series_id}_{check_name}"
                results[check_id] = _run_quality_check(
                    check_id,
                    check_fn,
                    source_id="fred",
                    series_id=series_id,
                    frequency=frequency,
                )

    for check_id, check_fn in DATA_QUALITY_CHECKS.items():
        if check_id.startswith("fred_"):
            continue
        source_id = check_id.rsplit("_", 1)[0]
        if not _source_enabled(config, source_id):
            continue
        results[check_id] = _run_quality_check(
            check_id,
            lambda check_fn=check_fn: check_fn(config),
            source_id=source_id,
        )
    return results
