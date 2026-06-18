import csv
import io
import time
from datetime import datetime, timezone

from http_client import make_request


class ConfiguredMacroCollector:
    """Thin adapter for official CSV/JSON time-series endpoints."""

    source_id = ""

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        source = config["collectors"][self.source_id]
        records = []
        for series in source.get("series", []):
            response = make_request(
                "GET",
                series["url"],
                params=series.get("params"),
                headers=source.get("headers"),
                correlation_id=correlation_id,
            )
            response.raise_for_status()
            records.extend(self._parse(response, series))
        return records

    def _parse(self, response, series: dict) -> list[dict]:
        fmt = series.get("format", "json")
        rows = (
            list(csv.DictReader(io.StringIO(response.text)))
            if fmt == "csv"
            else self._json_rows(response.json(), series.get("records_path", []))
        )
        output = []
        for row in rows:
            try:
                observed = datetime.fromisoformat(
                    str(row[series["date_field"]]).replace("Z", "+00:00")
                )
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                value = float(row[series["value_field"]])
            except (KeyError, TypeError, ValueError):
                continue
            output.append(
                {
                    "series_id": f"{self.source_id.upper()}:{series['id']}",
                    "observed_at": observed,
                    "value": value,
                    "source": self.source_id,
                    "released_at": self._optional_time(row, series.get("release_field")),
                    "revision_at": self._optional_time(row, series.get("revision_field")),
                    "metadata": {
                        "frequency": series.get("frequency"),
                        "semantic_feature": series.get("semantic_feature"),
                        "region": series.get("region"),
                        "title": series.get("title", series["id"]),
                    },
                }
            )
        return output

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
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def health_check(self, config: dict) -> dict:
        started = time.monotonic()
        series = config["collectors"][self.source_id].get("series", [])
        if not series:
            return {"healthy": False, "message": "No series configured", "latency_ms": 0}
        try:
            response = make_request("GET", series[0]["url"], timeout=15)
            return {
                "healthy": response.status_code < 400,
                "message": f"HTTP {response.status_code}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {"healthy": False, "message": str(exc), "latency_ms": int((time.monotonic() - started) * 1000)}

    def get_schedule(self, config): return config["collectors"][self.source_id]["schedule"]
    def get_target_table(self): return "macro_series"
    def get_conflict_columns(self): return ["series_id", "observed_at"]


class OecdCollector(ConfiguredMacroCollector): source_id = "oecd"
class EcbCollector(ConfiguredMacroCollector): source_id = "ecb"
class BoeCollector(ConfiguredMacroCollector): source_id = "boe"
class EiaCollector(ConfiguredMacroCollector): source_id = "eia"
