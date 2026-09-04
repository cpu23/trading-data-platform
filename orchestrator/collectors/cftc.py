import time
from datetime import UTC, date, datetime, timedelta

from errors import ERROR_CLASS_UNKNOWN, InvalidSourceData, classify_error
from http_client import make_request
from http_errors import safe_error_message
from logging_config import get_logger
from provider_origins import validate_configured_origin

from collectors.base import CollectionResult, CollectorNoData, CollectorSetupRequired

logger = get_logger("collector.cftc")

# CFTC contract market codes are official six-character alphanumeric strings
# (e.g. "099741" or "006NKJ"); they must never be coerced through int(), which
# would destroy leading zeroes and reject codes containing letters.
_POSITIONING_KIND = "futures_positioning"


def _normalize_code(value) -> str:
    """Normalize an official CFTC contract market code for comparison."""
    return str(value or "").strip().upper()


def _escape_soql(value: str) -> str:
    """Escape a SoQL string literal (single quotes are doubled)."""
    return value.replace("'", "''")


class CftcCollector:
    source_id = "cftc"

    def _configured_mappings(self, mappings):
        """Resolve operator mappings into normalized match sets.

        A mapping may identify a market by a single ``market_id`` or by an
        explicit ``market_ids`` list (e.g. old and new official codes for the
        same contract). Matching stays config-driven: nothing about
        code-to-asset relationships is hardcoded here. Optional fields:
        ``name`` (fallback match against ``contract_market_name`` when a row
        carries no code) and ``futonly_or_combined`` (restrict which report
        rows are ingested).
        """
        resolved = []
        for mapping in mappings or []:
            if not mapping.get("enabled", True):
                continue
            raw_ids = mapping.get("market_ids")
            if raw_ids is None:
                raw_ids = [mapping.get("market_id")] if mapping.get("market_id") else []
            ids = [_normalize_code(item) for item in raw_ids]
            ids = [item for item in ids if item]
            if not ids:
                continue
            resolved.append(
                {
                    "mapping": mapping,
                    "ids": frozenset(ids),
                    "name": _normalize_code(mapping.get("name")),
                    "futonly": _normalize_code(mapping.get("futonly_or_combined")),
                }
            )
        return resolved

    @staticmethod
    def _match_mapping(row, resolved):
        """Match one API row to a configured mapping, returning (mapping, market_id).

        Precedence: the row's official contract market code, then the
        contract market name (against configured ids or a mapping's ``name``).
        The first configured mapping that accepts the row wins, so a
        ``futonly_or_combined`` restriction can defer the row to a later
        mapping. The returned market_id is always the provider's own value.
        """
        row_code = _normalize_code(row.get("cftc_contract_market_code"))
        row_name = _normalize_code(row.get("contract_market_name"))
        candidates = []
        if row_code:
            candidates = [entry for entry in resolved if row_code in entry["ids"]]
            if not candidates and row_name:
                candidates = [
                    entry
                    for entry in resolved
                    if row_name in entry["ids"]
                    or (entry["name"] and row_name == entry["name"])
                ]
        elif row_name:
            candidates = [
                entry
                for entry in resolved
                if row_name in entry["ids"]
                or (entry["name"] and row_name == entry["name"])
            ]
        for entry in candidates:
            if not entry["futonly"] or (
                _normalize_code(row.get("futonly_or_combined")) == entry["futonly"]
            ):
                return entry, (row_code or row_name)
        return None, None

    def collect(self, config, correlation_id):
        cfg = config["collectors"]["cftc"]
        datasets = cfg.get("datasets") or []
        if not datasets:
            raise CollectorSetupRequired(
                "No CFTC datasets are configured",
                source_id=self.source_id,
            )
        try:
            lookback_days = int(cfg.get("lookback_days", 400))
        except (TypeError, ValueError):
            lookback_days = 400
        lookback_days = max(30, min(lookback_days, 3650))
        cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date()
        acquired_at = datetime.now(UTC)
        records: list[dict] = []
        dataset_errors: list[dict] = []
        configured_codes: set[str] = set()
        successful_datasets = 0
        first_error: Exception | None = None

        for dataset in datasets:
            resolved = self._configured_mappings(dataset.get("contracts", []))
            market_codes = {code for entry in resolved for code in entry["ids"]}
            configured_codes.update(market_codes)
            if not market_codes:
                dataset_errors.append(
                    {
                        "dataset": dataset.get("name"),
                        "stage": "configuration",
                        "code": "no_contracts",
                        "error_class": "invalid_source_data",
                    }
                )
                continue
            try:
                market_filter = (
                    "cftc_contract_market_code in("
                    + ",".join(
                        f"'{_escape_soql(code)}'" for code in sorted(market_codes)
                    )
                    + ")"
                )
                params = {
                    "$limit": dataset.get("limit", 5000),
                    "$order": "report_date_as_yyyy_mm_dd DESC",
                    "$where": (
                        market_filter
                        + " AND report_date_as_yyyy_mm_dd >= "
                        + f"'{cutoff.isoformat()}T00:00:00.000'"
                    ),
                }
                response = make_request(
                    "GET",
                    validate_configured_origin(
                        dataset["url"],
                        dataset,
                        label=f"CFTC {dataset.get('name')} url",
                        canonical={
                            "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
                            "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
                        },
                    ),
                    params=params,
                    correlation_id=correlation_id,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise InvalidSourceData("CFTC response must be a JSON array")
            except Exception as exc:
                policy = classify_error(exc)
                if policy.error_class == ERROR_CLASS_UNKNOWN:
                    raise
                if first_error is None:
                    first_error = exc
                dataset_errors.append(
                    {
                        "dataset": dataset.get("name"),
                        "stage": "dataset_fetch",
                        "code": policy.error_class,
                        "exception_type": type(exc).__name__,
                        "error_class": policy.error_class,
                    }
                )
                logger.error(
                    "cftc_dataset_failed",
                    dataset=dataset.get("name"),
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )
                continue

            dataset_records: list[dict] = []
            for row in payload:
                if not isinstance(row, dict):
                    continue
                matched = self._match_mapping(row, resolved)
                if matched[0] is None:
                    continue
                entry, market = matched
                report_date = row.get("report_date_as_yyyy_mm_dd")
                if not market or not report_date:
                    continue
                for category, long_key, short_key in dataset.get("categories", []):
                    try:
                        long_value = int(row[long_key])
                        short_value = int(row[short_key])
                        oi = int(row.get("open_interest_all") or 0)
                    except (KeyError, TypeError, ValueError):
                        continue
                    metadata = {
                        "positioning_kind": _POSITIONING_KIND,
                        "dataset": dataset.get("name"),
                        "semantics": dataset.get("semantics"),
                        "market_name": row.get("contract_market_name"),
                        "assets": entry["mapping"].get("assets", []),
                    }
                    for field in (
                        "cftc_market_code",
                        "cftc_region_code",
                        "cftc_commodity_code",
                        "futonly_or_combined",
                        "market_and_exchange_names",
                        "commodity_name",
                    ):
                        if row.get(field):
                            metadata[field] = row[field]
                    dataset_records.append(
                        {
                            "source": "cftc",
                            "market_id": market,
                            "report_date": date.fromisoformat(report_date[:10]),
                            "category": category,
                            "long_positions": long_value,
                            "short_positions": short_value,
                            "net_position": long_value - short_value,
                            "open_interest": oi,
                            "net_pct_open_interest": (
                                (long_value - short_value) / oi * 100 if oi else None
                            ),
                            "acquired_at": acquired_at,
                            "metadata": metadata,
                        }
                    )
            if dataset_records:
                records.extend(dataset_records)
                successful_datasets += 1
            else:
                dataset_errors.append(
                    {
                        "dataset": dataset.get("name"),
                        "stage": "dataset_parse",
                        "code": "no_data",
                        "error_class": "invalid_source_data",
                    }
                )

        if not records:
            if first_error is not None:
                raise first_error
            raise CollectorNoData(
                "CFTC returned no observations for mapped contracts",
                source_id=self.source_id,
                market_ids=sorted(configured_codes),
            )
        logger.info(
            "cftc_collection_completed",
            state="partial_failure" if dataset_errors else "success",
            market_ids=sorted(configured_codes),
            datasets_requested=len(datasets),
            datasets_succeeded=successful_datasets,
            records=len(records),
            acquired_at=acquired_at.isoformat(),
            correlation_id=correlation_id,
        )
        return CollectionResult(
            records=records,
            errors=dataset_errors,
            total_series=len(datasets),
            successful_series=successful_datasets,
            metrics={"api_calls_made": len(datasets)},
        )

    def health_check(self, config):
        started = time.monotonic()
        datasets = config["collectors"]["cftc"].get("datasets") or []
        if not datasets:
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No CFTC datasets are configured",
                "latency_ms": 0,
            }
        try:
            statuses = []
            for dataset in datasets:
                cftc_url = validate_configured_origin(
                    dataset["url"],
                    dataset,
                    label=f"CFTC {dataset.get('name')} url",
                    canonical={
                        "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
                        "https://publicreporting.cftc.gov/resource/72hh-3qpy.json",
                    },
                )
                response = make_request("GET", cftc_url, params={"$limit": 1})
                statuses.append(response.status_code)
            healthy = all(status == 200 for status in statuses)
            return {
                "healthy": healthy,
                "state": "success" if healthy else "failed",
                "message": f"HTTP {','.join(str(status) for status in statuses)}",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "state": "failed",
                "message": safe_error_message(exc, provider="cftc"),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_schedule(self, config):
        return config["collectors"]["cftc"]["schedule"]

    def get_target_table(self):
        return "positioning_reports"

    def get_conflict_columns(self):
        return ["source", "market_id", "report_date", "category"]
