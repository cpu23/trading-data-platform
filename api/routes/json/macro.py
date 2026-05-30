from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from config import load_config
from db import query_one, query_many
from staleness import get_staleness_config, is_stale

router = APIRouter()


def _fmt(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


@router.get("/macro/dashboard")
def get_macro_dashboard():
    config = load_config()
    thresholds = get_staleness_config(config)

    indicator_configs = config.get("dashboard", {}).get("indicators", [])
    if not indicator_configs:
        indicator_configs = [
            {"series_id": "T10Y2Y", "label": "10Y-2Y spread", "precision": 2, "category": "yield_curve"},
            {"series_id": "VIXCLS", "label": "VIX", "precision": 1, "category": "volatility"},
            {"series_id": "DTWEXBGS", "label": "USD index (broad)", "precision": 2, "category": "usd"},
            {"series_id": "BAMLH0A0HYM2", "label": "HY spread", "precision": 2, "category": "credit"},
            {"series_id": "DGS10", "label": "10Y yield", "precision": 2, "category": "rates"},
            {"series_id": "T5YIE", "label": "5Y breakeven", "precision": 2, "category": "inflation"},
        ]

    indicators = []
    for ic in indicator_configs:
        series_id = ic["series_id"]
        precision = ic.get("precision", 2)

        latest_sql = """
            SELECT observed_at, value FROM macro_series
            WHERE series_id = :sid
            ORDER BY observed_at DESC LIMIT 1
        """
        latest_row = query_one(latest_sql, params={"sid": series_id}, config=config)

        previous_sql = """
            SELECT observed_at, value FROM macro_series
            WHERE series_id = :sid
            ORDER BY observed_at DESC LIMIT 1 OFFSET 1
        """
        previous_row = query_one(previous_sql, params={"sid": series_id}, config=config)

        latest_value = None
        latest_observed_at = None
        previous_value = None
        change_abs = None
        change_pct = None
        trend_5d = None

        if latest_row:
            latest_value = round(latest_row["value"], precision) if latest_row["value"] is not None else None
            latest_observed_at = latest_row["observed_at"].strftime("%Y-%m-%d") if hasattr(latest_row["observed_at"], "strftime") else str(latest_row["observed_at"])[:10]

        if previous_row:
            previous_value = round(previous_row["value"], precision) if previous_row["value"] is not None else None

        if latest_value is not None and previous_value is not None:
            change_abs = round(latest_value - previous_value, precision)
            if previous_value != 0:
                change_pct = round(((latest_value - previous_value) / abs(previous_value)) * 100, 2)

        trend_sql = """
            SELECT observed_at, value FROM macro_series
            WHERE series_id = :sid AND observed_at >= :cutoff
            ORDER BY observed_at ASC
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        trend_rows = query_many(trend_sql, params={"sid": series_id, "cutoff": cutoff}, config=config)
        if len(trend_rows) >= 2:
            first_val = trend_rows[0]["value"]
            last_val = trend_rows[-1]["value"]
            if first_val is not None and last_val is not None:
                if last_val > first_val:
                    trend_5d = "up"
                elif last_val < first_val:
                    trend_5d = "down"
                else:
                    trend_5d = "flat"

        stale, _ = is_stale(
            latest_row["observed_at"] if latest_row else None,
            thresholds["macro_hours"],
        )

        indicators.append({
            "series_id": series_id,
            "label": ic.get("label", series_id),
            "category": ic.get("category"),
            "latest_value": latest_value,
            "latest_observed_at": latest_observed_at,
            "previous_value": previous_value,
            "change_abs": change_abs,
            "change_pct": change_pct,
            "trend_5d": trend_5d,
            "stale": stale,
        })

    last_collector_sql = """
        SELECT started_at FROM collection_log
        WHERE collector = 'fred'
        ORDER BY started_at DESC LIMIT 1
    """
    last_collector = query_one(last_collector_sql, config=config)

    return {
        "indicators": indicators,
        "last_collector_run": _fmt(last_collector["started_at"]) if last_collector else None,
    }


@router.get("/macro/{series_id}")
def get_macro_series(
    series_id: str,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
):
    config = load_config()

    params: dict = {"sid": series_id}
    date_filter = ""

    if from_date:
        params["from_date"] = from_date
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
        raise HTTPException(status_code=404, detail=f"No data found for series {series_id}", headers={"X-Error-Code": "NOT_FOUND"})

    return {
        "series_id": series_id,
        "observations": [
            {
                "observed_at": _fmt(row["observed_at"]),
                "value": row["value"],
            }
            for row in rows
        ],
        "count": len(rows),
    }