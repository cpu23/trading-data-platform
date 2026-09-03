from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

import config as app_config
from db import query_many
from serializers import isoformat
from staleness import get_staleness_config, is_stale

router = APIRouter()


@router.get("/macro/dashboard")
def get_macro_dashboard():
    config = app_config.load_config()
    thresholds = get_staleness_config(config)

    indicator_configs = config.get("dashboard", {}).get("indicators", [])
    if not indicator_configs:
        indicator_configs = [
            {
                "series_id": "T10Y2Y",
                "label": "10Y-2Y spread",
                "precision": 2,
                "category": "yield_curve",
            },
            {
                "series_id": "VIXCLS",
                "label": "VIX",
                "precision": 1,
                "category": "volatility",
            },
            {
                "series_id": "DTWEXBGS",
                "label": "USD index (broad)",
                "precision": 2,
                "category": "usd",
            },
            {
                "series_id": "BAMLH0A0HYM2",
                "label": "HY spread",
                "precision": 2,
                "category": "credit",
            },
            {
                "series_id": "DGS10",
                "label": "10Y yield",
                "precision": 2,
                "category": "rates",
            },
            {
                "series_id": "T5YIE",
                "label": "5Y breakeven",
                "precision": 2,
                "category": "inflation",
            },
        ]

    series_ids = [item["series_id"] for item in indicator_configs]
    rows = query_many(
        """
        WITH requested(series_id) AS (
            SELECT unnest(CAST(:series_ids AS TEXT[]))
        )
        SELECT requested.series_id,
               latest.observed_at AS latest_observed_at,
               latest.value AS latest_value,
               previous.value AS previous_value,
               trend.sample_count AS trend_sample_count,
               trend.first_value AS trend_first_value,
               trend.last_value AS trend_last_value,
               (
                   SELECT started_at
                   FROM collection_log
                   WHERE collector = 'fred'
                   ORDER BY started_at DESC
                   LIMIT 1
               ) AS last_collector_run
        FROM requested
        LEFT JOIN LATERAL (
            SELECT observed_at, value
            FROM macro_series
            WHERE series_id = requested.series_id
            ORDER BY observed_at DESC
            LIMIT 1
        ) latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT value
            FROM macro_series
            WHERE series_id = requested.series_id
            ORDER BY observed_at DESC
            LIMIT 1 OFFSET 1
        ) previous ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS sample_count,
                   (ARRAY_AGG(value ORDER BY observed_at ASC))[1] AS first_value,
                   (ARRAY_AGG(value ORDER BY observed_at DESC))[1] AS last_value
            FROM macro_series
            WHERE series_id = requested.series_id
              AND observed_at >= :cutoff
        ) trend ON TRUE
        """,
        params={
            "series_ids": series_ids,
            "cutoff": datetime.now(UTC) - timedelta(days=5),
        },
        config=config,
    )
    data_by_series = {row["series_id"]: row for row in rows}

    indicators = []
    for item in indicator_configs:
        series_id = item["series_id"]
        precision = item.get("precision", 2)
        row = data_by_series.get(series_id, {})
        latest = row.get("latest_value")
        previous = row.get("previous_value")
        latest_value = round(latest, precision) if latest is not None else None
        previous_value = round(previous, precision) if previous is not None else None
        latest_at = row.get("latest_observed_at")

        change_abs = None
        change_pct = None
        if latest_value is not None and previous_value is not None:
            change_abs = round(latest_value - previous_value, precision)
            if previous_value != 0:
                change_pct = round(
                    ((latest_value - previous_value) / abs(previous_value)) * 100,
                    2,
                )

        trend_5d = None
        if (row.get("trend_sample_count") or 0) >= 2:
            first_value = row.get("trend_first_value")
            last_value = row.get("trend_last_value")
            if first_value is not None and last_value is not None:
                if last_value > first_value:
                    trend_5d = "up"
                elif last_value < first_value:
                    trend_5d = "down"
                else:
                    trend_5d = "flat"

        stale, _ = is_stale(latest_at, thresholds["macro_hours"])
        indicators.append(
            {
                "series_id": series_id,
                "label": item.get("label", series_id),
                "category": item.get("category"),
                "latest_value": latest_value,
                "latest_observed_at": (
                    latest_at.strftime("%Y-%m-%d")
                    if hasattr(latest_at, "strftime")
                    else str(latest_at)[:10]
                    if latest_at is not None
                    else None
                ),
                "previous_value": previous_value,
                "change_abs": change_abs,
                "change_pct": change_pct,
                "trend_5d": trend_5d,
                "stale": stale,
            }
        )

    last_collector_run = rows[0].get("last_collector_run") if rows else None
    return {
        "indicators": indicators,
        "last_collector_run": isoformat(last_collector_run),
    }


@router.get("/macro/{series_id}")
def get_macro_series(
    series_id: str,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    days: int | None = Query(default=None, ge=1, le=3650),
):
    config = app_config.load_config()

    params: dict = {"sid": series_id}
    date_filter = ""

    if from_date:
        params["from_date"] = from_date
        date_filter += " AND observed_at >= :from_date"
    elif days:
        params["from_date"] = date.today() - timedelta(days=days)
        date_filter += " AND observed_at >= :from_date"
    else:
        default_from = date.today() - timedelta(days=365)
        params["from_date"] = default_from
        date_filter += " AND observed_at >= :from_date"

    if to_date:
        params["to_date"] = to_date
        date_filter += " AND observed_at <= :to_date"

    sql = f"""
        SELECT observed_at, value FROM macro_series
        WHERE series_id = :sid{date_filter}
        ORDER BY observed_at ASC
    """
    rows = query_many(sql, params=params, config=config)

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No data found for series {series_id}",
            headers={"X-Error-Code": "NOT_FOUND"},
        )

    return {
        "series_id": series_id,
        "observations": [
            {
                "observed_at": isoformat(row["observed_at"]),
                "value": row["value"],
            }
            for row in rows
        ],
        "count": len(rows),
    }
