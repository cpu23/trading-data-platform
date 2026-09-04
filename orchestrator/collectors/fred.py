import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from errors import InvalidSourceData, PersistenceError, TransientSourceError
from http_client import make_request
from http_errors import safe_error_message
from logging_config import get_logger
from sqlalchemy import text

from collectors.base import CollectionResult, elapsed_ms
from db import get_session, query_latest

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

REVISION_WINDOW_DAYS = {
    "daily": 14,
    "weekly": 90,
    "monthly": 365,
    "quarterly": 730,
    "annual": 730,
}


def _failure_class(code: str) -> str:
    if code in {"metadata_request_failed", "request_failed"}:
        return TransientSourceError.error_class
    if code == "cache_degraded":
        return PersistenceError.error_class
    return InvalidSourceData.error_class


@dataclass(frozen=True)
class MetadataOutcome:
    metadata: dict[str, Any] | None
    api_calls: int = 0
    warning: dict | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class PreparationFailure:
    stage: str
    code: str
    exception_type: str


@dataclass(frozen=True)
class ObservationFetchOutcome:
    observations: tuple[Any, ...]
    worker_duration_ms: int
    error_code: str | None = None
    exception_type: str | None = None


@dataclass(frozen=True)
class NormalizationOutcome:
    records: tuple[dict, ...]
    error_code: str | None = None
    exception_type: str | None = None


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
            "weekly": fred_config.get(
                "backfill_years_weekly", BACKFILL_YEARS["weekly"]
            ),
            "monthly": fred_config.get(
                "backfill_years_monthly", BACKFILL_YEARS["monthly"]
            ),
            "quarterly": fred_config.get(
                "backfill_years_quarterly", BACKFILL_YEARS["quarterly"]
            ),
        }

        all_records: list[dict] = []
        errors: list[dict] = []
        total_series = len(series_list)
        successful_series = 0
        metadata_duration_ms = 0
        metadata_api_calls = 0
        observation_api_calls = 0

        # Keep database-backed metadata resolution on the coordinator thread.
        prepared: list[tuple[dict, datetime, dict[str, Any]] | PreparationFailure] = []
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
                    metadata_outcome = self._resolve_series_metadata(
                        series_id, api_key, correlation_id, config
                    )
                finally:
                    metadata_duration_ms += elapsed_ms(metadata_started)
                metadata_api_calls += metadata_outcome.api_calls
                if metadata_outcome.warning is not None:
                    errors.append(metadata_outcome.warning)
                if metadata_outcome.error is not None:
                    prepared.append(
                        PreparationFailure(
                            stage="metadata",
                            code="metadata_request_failed",
                            exception_type=type(metadata_outcome.error).__name__,
                        )
                    )
                else:
                    prepared.append(
                        (series_entry, start_date, metadata_outcome.metadata or {})
                    )
            except Exception as exc:
                prepared.append(
                    PreparationFailure(
                        stage="metadata",
                        code="metadata_resolution_failed",
                        exception_type=type(exc).__name__,
                    )
                )

        futures: list[Future | PreparationFailure] = []
        fetch_outcomes: list[ObservationFetchOutcome | PreparationFailure] = []
        observation_started = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=self._max_concurrency(fred_config),
            thread_name_prefix="fred-observation",
        ) as executor:
            for item in prepared:
                if isinstance(item, PreparationFailure):
                    futures.append(item)
                    continue
                series_entry, start_date, _metadata = item
                futures.append(
                    executor.submit(
                        self._fetch_observations,
                        series_entry["id"],
                        api_key,
                        start_date,
                        correlation_id,
                    )
                )
                observation_api_calls += 1

            # Resolve every submitted fetch before normalization begins. Outcomes remain
            # in configured order even when worker completion order differs.
            for future in futures:
                if isinstance(future, PreparationFailure):
                    fetch_outcomes.append(future)
                    continue
                try:
                    fetch_outcomes.append(future.result())
                except Exception as exc:
                    fetch_outcomes.append(
                        ObservationFetchOutcome(
                            (),
                            0,
                            error_code="request_failed",
                            exception_type=type(exc).__name__,
                        )
                    )
        observation_duration_ms = elapsed_ms(observation_started)
        observation_worker_ms_total = sum(
            outcome.worker_duration_ms
            for outcome in fetch_outcomes
            if isinstance(outcome, ObservationFetchOutcome)
        )

        parse_started = time.monotonic()
        for series_entry, prepared_item, outcome in zip(
            series_list, prepared, fetch_outcomes, strict=True
        ):
            series_id = series_entry["id"]
            frequency = series_entry.get("frequency", "monthly")
            if isinstance(outcome, PreparationFailure):
                error_entry = {
                    "series_id": series_id,
                    "stage": outcome.stage,
                    "code": outcome.code,
                    "exception_type": outcome.exception_type,
                    "frequency": frequency,
                    "error_class": _failure_class(outcome.code),
                }
                errors.append(error_entry)
            elif outcome.error_code is not None:
                error_entry = {
                    "series_id": series_id,
                    "stage": "observation",
                    "code": outcome.error_code,
                    "exception_type": outcome.exception_type or "Exception",
                    "frequency": frequency,
                    "error_class": _failure_class(outcome.error_code),
                }
                errors.append(error_entry)
            else:
                assert not isinstance(prepared_item, PreparationFailure)
                _entry, _start_date, metadata = prepared_item
                normalized = self._normalize_observations(
                    series_id, frequency, metadata, outcome.observations
                )
                if normalized.error_code is not None:
                    error_entry = {
                        "series_id": series_id,
                        "stage": "observation",
                        "code": normalized.error_code,
                        "exception_type": normalized.exception_type or "Exception",
                        "frequency": frequency,
                        "error_class": _failure_class(normalized.error_code),
                    }
                    errors.append(error_entry)
                else:
                    all_records.extend(normalized.records)
                    successful_series += 1
                    logger.info(
                        "series_collected",
                        action="collect_series",
                        series_id=series_id,
                        records_fetched=len(normalized.records),
                        correlation_id=correlation_id,
                    )
                    continue

            logger.error(
                "series_collection_failed",
                action="collect_series",
                series_id=series_id,
                code=error_entry["code"],
                exception_type=error_entry["exception_type"],
                correlation_id=correlation_id,
            )
        parse_duration_ms = elapsed_ms(parse_started)

        self.last_errors = errors

        result = CollectionResult(
            records=all_records,
            errors=errors,
            total_series=total_series,
            successful_series=successful_series,
            metrics={
                "metadata_cache_duration_ms": metadata_duration_ms,
                "observation_fetch_duration_ms": observation_duration_ms,
                "observation_fetch_worker_ms_total": observation_worker_ms_total,
                "parse_normalize_duration_ms": parse_duration_ms,
                "metadata_api_calls": metadata_api_calls,
                "observation_api_calls": observation_api_calls,
                "api_calls_made": metadata_api_calls + observation_api_calls,
            },
        )

        logger.info(
            "fred_stage_metrics",
            action="collect",
            correlation_id=correlation_id,
            **result.metrics,
        )

        if errors:
            failed_series = len(
                {error.get("series_id") for error in errors if error.get("series_id")}
            )
            logger.warning(
                "fred_collection_partial",
                action="collect",
                total_series=total_series,
                successful_series=successful_series,
                failed_series=failed_series,
                correlation_id=correlation_id,
            )

        return result

    def _fetch_observations(
        self,
        series_id: str,
        api_key: str,
        start_date: datetime,
        correlation_id: str,
    ) -> ObservationFetchOutcome:
        """Perform only observation HTTP and response decoding on a worker thread."""

        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start_date.strftime("%Y-%m-%d"),
            "output_type": 3,
            "realtime_start": start_date.strftime("%Y-%m-%d"),
            "realtime_end": datetime.now(UTC).date().isoformat(),
        }
        started = time.monotonic()
        try:
            response = make_request(
                method="GET",
                url=FRED_OBSERVATIONS_URL,
                params=params,
                correlation_id=correlation_id,
            )
            response.raise_for_status()
            data = response.json()
            observations = tuple(data.get("observations", []))
        except Exception as exc:
            return ObservationFetchOutcome(
                (),
                elapsed_ms(started),
                error_code="request_failed",
                exception_type=type(exc).__name__,
            )
        return ObservationFetchOutcome(observations, elapsed_ms(started))

    def _normalize_observations(
        self,
        series_id: str,
        frequency: str,
        metadata: dict[str, Any],
        observations: tuple[Any, ...],
    ) -> NormalizationOutcome:
        """Normalize one fetched series on the coordinator thread."""

        try:
            records = []
            for obs in observations:
                date_str = obs.get("date", "")
                try:
                    observed_at = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    continue

                versions: list[tuple[datetime | None, Any]] = []
                if "value" in obs:
                    versions.append((None, obs.get("value")))
                else:
                    prefix = f"{series_id}_"
                    for key, raw_value in obs.items():
                        if not str(key).startswith(prefix):
                            continue
                        vintage_text = str(key)[len(prefix) :]
                        try:
                            vintage_at = datetime.strptime(
                                vintage_text, "%Y%m%d"
                            ).replace(tzinfo=UTC)
                        except ValueError:
                            continue
                        versions.append((vintage_at, raw_value))
                    versions.sort(
                        key=lambda item: item[0] or datetime.min.replace(tzinfo=UTC)
                    )

                for revision_number, (vintage_at, value_str) in enumerate(versions):
                    if value_str == "." or value_str is None:
                        continue
                    try:
                        value = float(value_str)
                    except (ValueError, TypeError):
                        continue
                    records.append(
                        {
                            "series_id": series_id,
                            "observed_at": observed_at,
                            "value": value,
                            "source": "fred",
                            "released_at": None,
                            "revision_at": vintage_at if revision_number > 0 else None,
                            "metadata": {
                                "units": metadata.get("units", ""),
                                "seasonal_adjustment": metadata.get(
                                    "seasonal_adjustment", ""
                                ),
                                "frequency": metadata.get("frequency", frequency),
                                "title": metadata.get("title", ""),
                            },
                        }
                    )
        except Exception as exc:
            return NormalizationOutcome(
                (), error_code="parse_failed", exception_type=type(exc).__name__
            )
        return NormalizationOutcome(tuple(records))

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
                    last_observed = datetime.fromisoformat(
                        last_observed.replace("Z", "+00:00")
                    )
                fred_config = config.get("collectors", {}).get("fred", {})
                configured_windows = fred_config.get("revision_window_days", {})
                try:
                    overlap_days = int(
                        configured_windows.get(
                            frequency, REVISION_WINDOW_DAYS.get(frequency, 30)
                        )
                    )
                except (TypeError, ValueError):
                    overlap_days = REVISION_WINDOW_DAYS.get(frequency, 30)
                return last_observed - timedelta(days=max(0, overlap_days))
        except Exception as exc:
            logger.warning(
                "latest_query_failed",
                action="get_start_date",
                series_id=series_id,
                error=safe_error_message(exc, provider="fred"),
            )

        return datetime.now(UTC) - timedelta(days=backfill_years * 365)

    @staticmethod
    def _metadata_values(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": row.get("title", ""),
            "units": row.get("units", ""),
            "seasonal_adjustment": row.get("seasonal_adjustment", ""),
            "frequency": row.get("frequency", ""),
        }

    @staticmethod
    def _fresh_metadata(row: dict[str, Any], ttl: timedelta, now: datetime) -> bool:
        fetched_at = row.get("fetched_at")
        if isinstance(fetched_at, str):
            try:
                fetched_at = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            except ValueError:
                return False
        if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
            return False
        return timedelta(0) <= now - fetched_at.astimezone(UTC) < ttl

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

    def _resolve_series_metadata(
        self, series_id: str, api_key: str, correlation_id: str, config: dict
    ) -> MetadataOutcome:
        fred_config = config.get("collectors", {}).get("fred", {})
        try:
            ttl_days = float(fred_config.get("metadata_ttl_days", 30))
        except (TypeError, ValueError):
            ttl_days = 30
        ttl = timedelta(days=max(ttl_days, 0))
        now = datetime.now(UTC)

        cached = self._metadata_cache.get(series_id)
        if cached is not None and self._fresh_metadata(cached, ttl, now):
            return MetadataOutcome(self._metadata_values(cached))

        warning = None
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
                return MetadataOutcome(self._metadata_values(persisted[0]))
        except Exception as exc:
            warning = {
                "series_id": series_id,
                "stage": "metadata_cache",
                "code": "cache_degraded",
                "exception_type": type(exc).__name__,
                "error_class": _failure_class("cache_degraded"),
            }
            logger.warning(
                "metadata_cache_read_failed",
                action="fetch_series_metadata",
                series_id=series_id,
                code=warning["code"],
                exception_type=warning["exception_type"],
                correlation_id=correlation_id,
            )

        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
        }

        try:
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
        except Exception as exc:
            return MetadataOutcome(None, api_calls=1, warning=warning, error=exc)

        fetched_at = datetime.now(UTC)
        persisted_successfully = False
        try:
            self._persist_series_metadata(series_id, metadata, fetched_at, config)
            persisted_successfully = True
        except Exception as exc:
            if warning is None:
                warning = {
                    "series_id": series_id,
                    "stage": "metadata_cache",
                    "code": "cache_degraded",
                    "exception_type": type(exc).__name__,
                    "error_class": _failure_class("cache_degraded"),
                }
            logger.warning(
                "metadata_cache_write_failed",
                action="fetch_series_metadata",
                series_id=series_id,
                code="cache_degraded",
                exception_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
        if persisted_successfully:
            self._metadata_cache[series_id] = {**metadata, "fetched_at": fetched_at}
        return MetadataOutcome(metadata, api_calls=1, warning=warning)

    def _fetch_series_metadata(
        self, series_id: str, api_key: str, correlation_id: str, config: dict
    ) -> dict[str, Any]:
        """Backward-compatible metadata-only interface used by focused callers."""
        outcome = self._resolve_series_metadata(
            series_id, api_key, correlation_id, config
        )
        if outcome.error is not None:
            raise outcome.error
        return outcome.metadata or {}

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
                "message": f"FRED API unreachable: {safe_error_message(exc, provider='fred')}",
                "latency_ms": latency_ms,
            }

    def get_target_table(self) -> str:
        return "macro_series"

    def get_conflict_columns(self) -> list[str]:
        return ["series_id", "observed_at"]
