import csv
import io
import time
from datetime import UTC, datetime

from collectors.base import CollectorNoData, CollectorSetupRequired
from http_client import make_request
from logging_config import get_logger
from provider_origins import validate_configured_origin

logger = get_logger("collector.official_macro")


class ConfiguredMacroCollector:
    """Thin adapter for official CSV/JSON time-series endpoints."""

    source_id = ""

    def __init__(self):
        self.last_result_metadata: dict = {}

    @staticmethod
    def _api_key(source: dict) -> str:
        return str(source.get("api_key") or source.get("public_api_key") or "").strip()

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        source = config["collectors"][self.source_id]
        series_list = source.get("series", [])
        api_key = self._api_key(source)
        if not series_list:
            raise CollectorSetupRequired(
                f"No {self.source_id.upper()} series configured",
                source_id=self.source_id,
            )
        if source.get("requires_api_key") and not api_key:
            raise CollectorSetupRequired(
                f"{self.source_id.upper()} API key is not configured",
                source_id=self.source_id,
                credential=source.get(
                    "credential_name", f"{self.source_id.upper()}_API_KEY"
                ),
            )

        acquired_at = datetime.now(UTC)
        records = []
        failures = []
        empty_series = []
        for series in series_list:
            try:
                params = dict(series.get("params") or {})
                if api_key:
                    params.setdefault(source.get("api_key_param", "api_key"), api_key)
                response = make_request(
                    "GET",
                    validate_configured_origin(
                        series["url"],
                        config["collectors"].get(self.source_id, {}),
                        label=f"{self.source_id} series",
                    ),
                    params=params or None,
                    headers=source.get("headers"),
                    correlation_id=correlation_id,
                    max_retries=source.get("max_retries", 3),
                )
                response.raise_for_status()
                parsed = self._parse(response, series, acquired_at=acquired_at)
                if not parsed:
                    empty_series.append(series["id"])
                records.extend(parsed)
            except Exception as exc:
                failures.append({"series_id": series["id"], "error": str(exc)})
                logger.error(
                    "official_series_failed",
                    source_id=self.source_id,
                    series_id=series["id"],
                    error=str(exc),
                    correlation_id=correlation_id,
                )

        state = "partial" if failures or empty_series else "success"
        if not records:
            raise CollectorNoData(
                f"{self.source_id.upper()} returned no observations",
                source_id=self.source_id,
                failed_series=failures,
                empty_series=empty_series,
            )
        self.last_result_metadata = {
            "state": state,
            "source_id": self.source_id,
            "series_configured": len(series_list),
            "series_failed": failures,
            "series_empty": empty_series,
            "records": len(records),
            "acquired_at": acquired_at.isoformat(),
        }
        logger.info(
            "official_collection_completed",
            correlation_id=correlation_id,
            **self.last_result_metadata,
        )
        return records

    def _parse(
        self,
        response,
        series: dict,
        acquired_at: datetime | None = None,
    ) -> list[dict]:
        fmt = series.get("format", "json")
        rows = (
            list(csv.DictReader(io.StringIO(response.text)))
            if fmt == "csv"
            else self._json_rows(response.json(), series.get("records_path", []))
        )
        acquired_at = acquired_at or datetime.now(UTC)
        output = []
        for row in rows:
            try:
                observed = self._parse_time(row[series["date_field"]], series)
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                value = float(row[series["value_field"]])
            except (KeyError, TypeError, ValueError):
                continue
            output.append(
                {
                    "series_id": f"{self.source_id.upper()}:{series['id']}",
                    "observed_at": observed,
                    "value": value,
                    "source": self.source_id,
                    "released_at": self._optional_time(
                        row, series.get("release_field")
                    ),
                    "revision_at": self._optional_time(
                        row, series.get("revision_field")
                    ),
                    "acquired_at": acquired_at,
                    "metadata": {
                        "frequency": series.get("frequency"),
                        "semantic_feature": series.get("semantic_feature"),
                        "region": series.get("region"),
                        "title": series.get("title", series["id"]),
                        "provider_series": series.get("provider_series", series["id"]),
                    },
                }
            )
        return output

    @staticmethod
    def _parse_time(value, series):
        text_value = str(value).strip()
        date_format = series.get("date_format")
        if date_format:
            return datetime.strptime(text_value, date_format).replace(tzinfo=UTC)
        if len(text_value) == 7 and text_value[4] == "-":
            text_value = f"{text_value}-01"
        elif len(text_value) == 4 and text_value.isdigit():
            text_value = f"{text_value}-01-01"
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))

    @staticmethod
    def _json_rows(payload, path):
        current = payload
        for key in path:
            current = current[key]
        return current if isinstance(current, list) else []

    @staticmethod
    def _optional_time(row, field):
        if not field or not row.get(field):
            return None
        try:
            value = datetime.fromisoformat(str(row[field]).replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        except ValueError:
            return None

    def health_check(self, config: dict) -> dict:
        started = time.monotonic()
        series = config["collectors"][self.source_id].get("series", [])
        if not series:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No series configured",
                "latency_ms": 0,
            }
        source = config["collectors"][self.source_id]
        api_key = self._api_key(source)
        if source.get("requires_api_key") and not api_key:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": f"{self.source_id.upper()} API key is not configured",
                "latency_ms": 0,
            }
        try:
            params = dict(series[0].get("params") or {})
            if api_key:
                params.setdefault(source.get("api_key_param", "api_key"), api_key)
            response = make_request(
                "GET",
                validate_configured_origin(
                    series[0]["url"],
                    config["collectors"].get(self.source_id, {}),
                    label=f"{self.source_id} series",
                ),
                params=params or None,
                headers=source.get("headers"),
                timeout=15,
                max_retries=source.get("max_retries", 3),
            )
            return {
                "healthy": response.status_code < 400,
                "state": "success" if response.status_code < 400 else "failed",
                "message": f"HTTP {response.status_code}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": str(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_schedule(self, config):
        return config["collectors"][self.source_id]["schedule"]

    def get_target_table(self):
        return "macro_series"

    def get_conflict_columns(self):
        return ["series_id", "observed_at"]


class OecdCollector(ConfiguredMacroCollector):
    source_id = "oecd"


class EcbCollector(ConfiguredMacroCollector):
    source_id = "ecb"


class BoeCollector(ConfiguredMacroCollector):
    source_id = "boe"


class EiaCollector(ConfiguredMacroCollector):
    source_id = "eia"
