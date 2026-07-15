import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from collectors.base import CollectionResult, elapsed_ms
from db import get_session, query_latest
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
        self.last_errors: list[dict] = []

    @staticmethod
    def _max_concurrency(fred_config: dict) -> int:
        try:
            configured = int(fred_config.get("max_concurrency", 4))
        except (TypeError, ValueError):
            configured = 4
        return max(1, min(configured, 16))

    def collect(self, config: dict, correlation_id: str) -> CollectionResult:
        fred_config = config["collectors"]["fred"]
        api_key = fred_config["api_key"]
        series_list = fred_config["series"]

        backfill_overrides = {
            "daily": fred_config.get("backfill_years_daily", BACKFILL_YEARS["daily"]),
            "weekly": fred_config.get("backfill_years_weekly", BACKFILL_YEARS["weekly"]),
            "monthly": fred_config.get("backfill_years_monthly", BACKFILL_YEARS["monthly"]),
            "quarterly": fred_config.get("backfill_years_quarterly", BACKFILL_YEARS["quarterly"]),
        }

        all_records: list[dict] = []
        errors: list[dict] = []
        total_series = len(series_list)
        successful_series = 0
        metadata_duration_ms = 0
        observation_duration_ms = 0
        parse_duration_ms = 0

        # Keep every database-backed lookup on the caller thread. Worker threads only
        # perform HTTP and pure normalization, so SQLAlchemy sessions are never shared.
        prepared: list[tuple[dict, datetime, dict[str, Any]] | Exception] = []
        for series_entry in series_list:
            series_id = series_entry["id"]
            frequency = series_entry.get("frequency", "monthly")
            try:
                start_date = self._get_start_date(
                    series_id,
                    frequency,
                    backfill_overrides.get(frequency, 5),
                    config,
                )
                metadata_started = time.monotonic()
                try:
                    metadata = self._fetch_series_metadata(
                        series_id, api_key, correlation_id, config
                    )
                finally:
                    metadata_duration_ms += elapsed_ms(metadata_started)
                prepared.append((series_entry, start_date, metadata))
            except Exception as exc:
                prepared.append(exc)

        futures: list[Future | Exception] = []
        with ThreadPoolExecutor(
            max_workers=self._max_concurrency(fred_config),
            thread_name_prefix="fred-observation",
        ) as executor:
            for item in prepared:
                if isinstance(item, Exception):
                    futures.append(item)
                    continue
                series_entry, start_date, metadata = item
                futures.append(
                    executor.submit(
                        self._fetch_and_normalize_observations,
                        series_entry["id"],
                        series_entry.get("frequency", "monthly"),
                        api_key,
                        start_date,
                        metadata,
                        correlation_id,
                    )
                )

            # Futures are all submitted before results are consumed. Reading them in
            # configured order gives deterministic records and never cancels later work.
            for series_entry, future in zip(series_list, futures, strict=True):
                series_id = series_entry["id"]
                frequency = series_entry.get("frequency", "monthly")
                try:
                    if isinstance(future, Exception):
                        raise future
                    records, fetch_ms, normalize_ms = future.result()
                    observation_duration_ms += fetch_ms
                    parse_duration_ms += normalize_ms
                    all_records.extend(records)
                    successful_series += 1
                    logger.info(
                        "series_collected",
                        action="collect_series",
                        series_id=series_id,
                        records_fetched=len(records),
                        correlation_id=correlation_id,
                    )
                except Exception as exc:
                    error_entry = {
                        "series_id": series_id,
                        "error": str(exc),
                        "frequency": frequency,
                    }
                    errors.append(error_entry)
                    logger.error(
                        "series_collection_failed",
                        action="collect_series",
                        series_id=series_id,
                        error=str(exc),
                        correlation_id=correlation_id,
                    )

        self.last_errors = errors

        result = CollectionResult(
            records=all_records,
            errors=errors,
            total_series=total_series,
            successful_series=successful_series,
            metrics={
                "metadata_cache_duration_ms": metadata_duration_ms,
                "observation_fetch_duration_ms": observation_duration_ms,
                "parse_normalize_duration_ms": parse_duration_ms,
            },
        )

        logger.info(
            "fred_stage_metrics",
            action="collect",
            correlation_id=correlation_id,
            **result.metrics,
        )

        if errors:
            logger.warning(
                "fred_collection_partial",
                action="collect",
                total_series=total_series,
                successful_series=successful_series,
                failed_series=len(errors),
                correlation_id=correlation_id,
            )

        return result

    def _fetch_and_normalize_observations(
        self,
        series_id: str,
        frequency: str,
        api_key: str,
        start_date: datetime,
        metadata: dict[str, Any],
        correlation_id: str,
    ) -> tuple[list[dict], int, int]:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_date.strftime("%Y-%m-%d"),
        }

        fetch_started = time.monotonic()
        response = make_request(
            method="GET",
            url=FRED_OBSERVATIONS_URL,
            params=params,
            correlation_id=correlation_id,
        )
        response.raise_for_status()

        data = response.json()
        observations = data.get("observations", [])
        fetch_duration_ms = elapsed_ms(fetch_started)

        parse_started = time.monotonic()
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
                "metadata": {
                    "units": metadata.get("units", ""),
                    "seasonal_adjustment": metadata.get("seasonal_adjustment", ""),
                    "frequency": metadata.get("frequency", frequency),
                    "title": metadata.get("title", ""),
                },
            }
            records.append(record)

        parse_duration_ms = elapsed_ms(parse_started)
        return records, fetch_duration_ms, parse_duration_ms

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
                return last_observed + timedelta(days=1)
        except Exception as exc:
            logger.warning(
                "latest_query_failed",
                action="get_start_date",
                series_id=series_id,
                error=str(exc),
            )

        return datetime.now(timezone.utc) - timedelta(days=backfill_years * 365)

    @staticmethod
    def _metadata_values(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": row.get("title", ""),
            "units": row.get("units", ""),
            "seasonal_adjustment": row.get("seasonal_adjustment", ""),
            "frequency": row.get("frequency", ""),
        }

    @staticmethod
    def _fresh_metadata(
        row: dict[str, Any], ttl: timedelta, now: datetime
    ) -> bool:
        fetched_at = row.get("fetched_at")
        if isinstance(fetched_at, str):
            try:
                fetched_at = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            except ValueError:
                return False
        if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
            return False
        return timedelta(0) <= now - fetched_at.astimezone(timezone.utc) < ttl

    def _persist_series_metadata(
        self,
        series_id: str,
        metadata: dict[str, Any],
        fetched_at: datetime,
        config: dict,
    ) -> None:
        statement = text(
            "INSERT INTO macro_series_metadata "
            "(series_id, title, units, seasonal_adjustment, frequency, fetched_at) "
            "VALUES (:series_id, :title, :units, :seasonal_adjustment, :frequency, :fetched_at) "
            "ON CONFLICT (series_id) DO UPDATE SET "
            "title = EXCLUDED.title, units = EXCLUDED.units, "
            "seasonal_adjustment = EXCLUDED.seasonal_adjustment, "
            "frequency = EXCLUDED.frequency, fetched_at = EXCLUDED.fetched_at"
        )
        params = {"series_id": series_id, **metadata, "fetched_at": fetched_at}
        with get_session(config) as session:
            session.execute(statement, params)

    def _fetch_series_metadata(
        self, series_id: str, api_key: str, correlation_id: str, config: dict
    ) -> dict[str, Any]:
        fred_config = config.get("collectors", {}).get("fred", {})
        try:
            ttl_days = float(fred_config.get("metadata_ttl_days", 30))
        except (TypeError, ValueError):
            ttl_days = 30
        ttl = timedelta(days=max(ttl_days, 0))
        now = datetime.now(timezone.utc)

        cached = self._metadata_cache.get(series_id)
        if cached is not None and self._fresh_metadata(cached, ttl, now):
            return self._metadata_values(cached)

        try:
            persisted = query_latest(
                table_name="macro_series_metadata",
                filters={"series_id": series_id},
                order_by="fetched_at DESC",
                limit=1,
                config=config,
            )
            if persisted and self._fresh_metadata(persisted[0], ttl, now):
                self._metadata_cache[series_id] = dict(persisted[0])
                return self._metadata_values(persisted[0])
        except Exception as exc:
            logger.warning(
                "metadata_cache_read_failed",
                action="fetch_series_metadata",
                series_id=series_id,
                error=str(exc),
                correlation_id=correlation_id,
            )

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

        fetched_at = datetime.now(timezone.utc)
        try:
            self._persist_series_metadata(series_id, metadata, fetched_at, config)
        except Exception as exc:
            logger.warning(
                "metadata_cache_write_failed",
                action="fetch_series_metadata",
                series_id=series_id,
                error=str(exc),
                correlation_id=correlation_id,
            )
        self._metadata_cache[series_id] = {**metadata, "fetched_at": fetched_at}
        return metadata

    def get_schedule(self, config: dict) -> str:
        return config["collectors"]["fred"]["schedule"]

    def health_check(self, config: dict) -> dict:
        api_key = config["collectors"]["fred"]["api_key"]
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
                    "message": "FRED API reachable",
                    "latency_ms": latency_ms,
                }
            else:
                return {
                    "healthy": False,
                    "message": f"FRED API returned status {response.status_code}",
                    "latency_ms": latency_ms,
                }
        except Exception as exc:
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            return {
                "healthy": False,
                "message": f"FRED API unreachable: {exc}",
                "latency_ms": latency_ms,
            }

    def get_target_table(self) -> str:
        return "macro_series"

    def get_conflict_columns(self) -> list[str]:
        return ["series_id", "observed_at"]
