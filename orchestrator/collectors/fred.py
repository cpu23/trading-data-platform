import time
from datetime import datetime, timedelta, timezone
from typing import Any

from collectors.base import CollectorNoData, CollectorSetupRequired
from db import query_latest
from http_client import make_request
from logging_config import get_logger

logger = get_logger("collector.fred")

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"

BACKFILL_YEARS = {
    "daily": 2,
    "weekly": 3,
    "monthly": 5,
    "quarterly": 10,
    "annual": 10,
}


class FredCollector:
    source_id = "fred"
    _metadata_cache: dict[str, dict[str, Any]]

    def __init__(self):
        self._metadata_cache = {}
        self.last_result_metadata: dict = {}

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        fred_config = config["collectors"]["fred"]
        api_key = fred_config.get("api_key", "")
        if not api_key:
            raise CollectorSetupRequired(
                "FRED API key is not configured",
                source_id=self.source_id,
                credential="FRED_API_KEY",
            )
        series_list = fred_config["series"]

        backfill_overrides = {
            "daily": fred_config.get("backfill_years_daily", BACKFILL_YEARS["daily"]),
            "weekly": fred_config.get("backfill_years_weekly", BACKFILL_YEARS["weekly"]),
            "monthly": fred_config.get("backfill_years_monthly", BACKFILL_YEARS["monthly"]),
            "quarterly": fred_config.get("backfill_years_quarterly", BACKFILL_YEARS["quarterly"]),
        }

        all_records: list[dict] = []
        failures = []
        empty_series = []
        acquired_at = datetime.now(timezone.utc)

        for series_entry in series_list:
            series_id = series_entry["id"]
            frequency = series_entry.get("frequency", "monthly")

            try:
                records = self._collect_series(
                    series_id=series_id,
                    frequency=frequency,
                    api_key=api_key,
                    backfill_years=backfill_overrides.get(frequency, 5),
                    correlation_id=correlation_id,
                    config=config,
                    acquired_at=acquired_at,
                )
                all_records.extend(records)
                if not records:
                    empty_series.append(series_id)
                logger.info(
                    "series_collected",
                    action="collect_series",
                    series_id=series_id,
                    records_fetched=len(records),
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                failures.append({"series_id": series_id, "error": str(exc)})
                logger.error(
                    "series_collection_failed",
                    action="collect_series",
                    series_id=series_id,
                    error=str(exc),
                    correlation_id=correlation_id,
                )

        if not all_records:
            raise CollectorNoData(
                "FRED returned no observations",
                source_id=self.source_id,
                failed_series=failures,
                empty_series=empty_series,
            )
        self.last_result_metadata = {
            "state": "partial" if failures or empty_series else "success",
            "source_id": self.source_id,
            "series_configured": len(series_list),
            "series_failed": failures,
            "series_empty": empty_series,
            "records": len(all_records),
            "acquired_at": acquired_at.isoformat(),
        }
        logger.info(
            "fred_collection_completed",
            correlation_id=correlation_id,
            **self.last_result_metadata,
        )
        return all_records

    def _collect_series(
        self,
        series_id: str,
        frequency: str,
        api_key: str,
        backfill_years: int,
        correlation_id: str,
        config: dict,
        acquired_at: datetime,
    ) -> list[dict]:
        start_date = self._get_start_date(series_id, frequency, backfill_years, config)

        metadata = self._fetch_series_metadata(series_id, api_key, correlation_id)

        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_date.strftime("%Y-%m-%d"),
        }

        response = make_request(
            method="GET",
            url=FRED_OBSERVATIONS_URL,
            params=params,
            correlation_id=correlation_id,
        )
        response.raise_for_status()

        data = response.json()
        observations = data.get("observations", [])

        records = []
        for obs in observations:
            value_str = obs.get("value", ".")
            if value_str == "." or value_str is None:
                continue

            try:
                value = float(value_str)
            except (ValueError, TypeError):
                continue

            date_str = obs.get("date", "")
            try:
                observed_at = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            record = {
                "series_id": series_id,
                "observed_at": observed_at,
                "value": value,
                "source": "fred",
                "acquired_at": acquired_at,
                "metadata": {
                    "units": metadata.get("units", ""),
                    "seasonal_adjustment": metadata.get("seasonal_adjustment", ""),
                    "frequency": metadata.get("frequency", frequency),
                    "title": metadata.get("title", ""),
                },
            }
            records.append(record)

        return records

    def _get_start_date(
        self,
        series_id: str,
        frequency: str,
        backfill_years: int,
        config: dict,
    ) -> datetime:
        try:
            latest = query_latest(
                table_name="macro_series",
                filters={"series_id": series_id},
                order_by="observed_at DESC",
                limit=1,
                config=config,
            )
            if latest:
                last_observed = latest[0]["observed_at"]
                if isinstance(last_observed, str):
                    last_observed = datetime.fromisoformat(last_observed.replace("Z", "+00:00"))
                revision_days = (
                    config.get("collectors", {})
                    .get("fred", {})
                    .get(
                        "revision_window_days",
                        {
                            "daily": 14,
                            "weekly": 35,
                            "monthly": 120,
                            "quarterly": 550,
                            "annual": 730,
                        },
                    )
                    .get(frequency, 120)
                )
                return last_observed - timedelta(days=revision_days)
        except Exception as exc:
            logger.warning(
                "latest_query_failed",
                action="get_start_date",
                series_id=series_id,
                error=str(exc),
            )

        return datetime.now(timezone.utc) - timedelta(days=backfill_years * 365)

    def _fetch_series_metadata(
        self, series_id: str, api_key: str, correlation_id: str
    ) -> dict[str, Any]:
        if series_id in self._metadata_cache:
            return self._metadata_cache[series_id]

        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
        }

        response = make_request(
            method="GET",
            url=FRED_SERIES_URL,
            params=params,
            correlation_id=correlation_id,
        )
        response.raise_for_status()

        data = response.json()
        ser = data.get("seriess", [{}])[0] if data.get("seriess") else {}

        metadata = {
            "title": ser.get("title", ""),
            "units": ser.get("units", ""),
            "seasonal_adjustment": ser.get("seasonal_adjustment", ""),
            "frequency": ser.get("frequency", ""),
        }

        self._metadata_cache[series_id] = metadata
        return metadata

    def get_schedule(self, config: dict) -> str:
        return config["collectors"]["fred"]["schedule"]

    def health_check(self, config: dict) -> dict:
        api_key = config["collectors"]["fred"].get("api_key", "")
        if not api_key:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "FRED_API_KEY is not set",
                "latency_ms": 0,
            }
        start_ms = time.monotonic() * 1000

        try:
            params = {
                "series_id": "GDP",
                "api_key": api_key,
                "file_type": "json",
            }
            response = make_request(
                method="GET",
                url=FRED_SERIES_URL,
                params=params,
                correlation_id="health-check",
            )
            latency_ms = int(time.monotonic() * 1000 - start_ms)

            if response.status_code == 200:
                return {
                    "healthy": True,
                    "state": "success",
                    "message": "FRED API reachable",
                    "latency_ms": latency_ms,
                }
            else:
                return {
                    "healthy": False,
                    "state": "failed",
                    "message": f"FRED API returned status {response.status_code}",
                    "latency_ms": latency_ms,
                }
        except Exception as exc:
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            return {
                "healthy": False,
                "state": "failed",
                "message": f"FRED API unreachable: {exc}",
                "latency_ms": latency_ms,
            }

    def get_target_table(self) -> str:
        return "macro_series"

    def get_conflict_columns(self) -> list[str]:
        return ["series_id", "observed_at"]
