import time
from datetime import UTC, date, datetime, timedelta

from collectors.base import CollectorNoData, CollectorSetupRequired
from http_client import make_request
from logging_config import get_logger
from provider_origins import validate_configured_origin

logger = get_logger("collector.cftc")


class CftcCollector:
    source_id = "cftc"

    def collect(self, config, correlation_id):
        cfg = config["collectors"]["cftc"]
        mappings = cfg.get("contracts", [])
        market_codes = {
            mapping["market_id"] for mapping in mappings if mapping.get("enabled", True)
        }
        if not market_codes:
            raise CollectorSetupRequired(
                "No CFTC contracts are mapped to configured assets",
                source_id=self.source_id,
            )
        try:
            lookback_days = int(cfg.get("lookback_days", 400))
        except (TypeError, ValueError):
            lookback_days = 400
        lookback_days = max(30, min(lookback_days, 3650))
        cutoff = (datetime.now(UTC) - timedelta(days=lookback_days)).date()
        market_filter = (
            "cftc_contract_market_code in("
            + ",".join(f"'{code}'" for code in sorted(market_codes))
            + ")"
        )
        params = {
            "$limit": cfg.get("limit", 5000),
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$where": (
                market_filter
                + f" AND report_date_as_yyyy_mm_dd >= '{cutoff.isoformat()}T00:00:00.000'"
            ),
        }
        response = make_request(
            "GET",
            validate_configured_origin(
                cfg["url"],
                cfg,
                label="CFTC url",
                canonical={"https://publicreporting.cftc.gov/resource/gpe5-46if.json"},
            ),
            params=params,
            correlation_id=correlation_id,
        )
        response.raise_for_status()
        records = []
        acquired_at = datetime.now(UTC)
        mapping_by_market = {item["market_id"]: item for item in mappings}
        for row in response.json():
            market = row.get("cftc_contract_market_code") or row.get(
                "contract_market_name"
            )
            if market not in market_codes:
                continue
            report_date = row.get("report_date_as_yyyy_mm_dd")
            if not market or not report_date:
                continue
            for category, long_key, short_key in cfg.get("categories", []):
                try:
                    long_value, short_value = int(row[long_key]), int(row[short_key])
                    oi = int(row.get("open_interest_all") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                records.append(
                    {
                        "source": "cftc",
                        "market_id": market,
                        "report_date": date.fromisoformat(report_date[:10]),
                        "category": category,
                        "long_positions": long_value,
                        "short_positions": short_value,
                        "net_position": long_value - short_value,
                        "open_interest": oi,
                        "net_pct_open_interest": ((long_value - short_value) / oi * 100)
                        if oi
                        else None,
                        "acquired_at": acquired_at,
                        "metadata": {
                            "market_name": row.get("contract_market_name"),
                            "assets": mapping_by_market.get(market, {}).get(
                                "assets", []
                            ),
                        },
                    }
                )
        if not records:
            raise CollectorNoData(
                "CFTC returned no observations for mapped contracts",
                source_id=self.source_id,
                market_ids=sorted(market_codes),
            )
        logger.info(
            "cftc_collection_completed",
            state="success",
            market_ids=sorted(market_codes),
            records=len(records),
            acquired_at=acquired_at.isoformat(),
            correlation_id=correlation_id,
        )
        return records

    def health_check(self, config):
        started = time.monotonic()
        mappings = config["collectors"]["cftc"].get("contracts", [])
        if not any(item.get("enabled", True) for item in mappings):
            return {
                "healthy": False,
                "state": "setup_required",
                "message": "No CFTC contracts are mapped",
                "latency_ms": 0,
            }
        try:
            cftc_url = validate_configured_origin(
                config["collectors"]["cftc"]["url"],
                config["collectors"]["cftc"],
                label="CFTC url",
                canonical={"https://publicreporting.cftc.gov/resource/gpe5-46if.json"},
            )
            response = make_request(
                "GET", cftc_url, params={"$limit": 1}
            )
            return {
                "healthy": response.status_code == 200,
                "state": "success" if response.status_code == 200 else "failed",
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
        return config["collectors"]["cftc"]["schedule"]

    def get_target_table(self):
        return "positioning_reports"

    def get_conflict_columns(self):
        return ["source", "market_id", "report_date", "category"]
