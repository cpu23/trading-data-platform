import time
from datetime import date

from http_client import make_request


class CftcCollector:
    source_id = "cftc"

    def collect(self, config, correlation_id):
        cfg = config["collectors"]["cftc"]
        response = make_request("GET", cfg["url"], params={"$limit": cfg.get("limit", 5000)}, correlation_id=correlation_id)
        response.raise_for_status()
        records = []
        for row in response.json():
            market = row.get("cftc_contract_market_code") or row.get("contract_market_name")
            report_date = row.get("report_date_as_yyyy_mm_dd")
            if not market or not report_date:
                continue
            for category, long_key, short_key in cfg.get("categories", []):
                try:
                    long_value, short_value = int(row[long_key]), int(row[short_key])
                    oi = int(row.get("open_interest_all") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                records.append({
                    "source": "cftc", "market_id": market,
                    "report_date": date.fromisoformat(report_date[:10]),
                    "category": category, "long_positions": long_value,
                    "short_positions": short_value, "net_position": long_value - short_value,
                    "open_interest": oi,
                    "net_pct_open_interest": ((long_value - short_value) / oi * 100) if oi else None,
                    "metadata": {"market_name": row.get("contract_market_name")},
                })
        return records

    def health_check(self, config):
        started = time.monotonic()
        try:
            response = make_request("GET", config["collectors"]["cftc"]["url"], params={"$limit": 1})
            return {"healthy": response.status_code == 200, "message": f"HTTP {response.status_code}", "latency_ms": int((time.monotonic()-started)*1000)}
        except Exception as exc:
            return {"healthy": False, "message": str(exc), "latency_ms": int((time.monotonic()-started)*1000)}

    def get_schedule(self, config): return config["collectors"]["cftc"]["schedule"]
    def get_target_table(self): return "positioning_reports"
    def get_conflict_columns(self): return ["source", "market_id", "report_date", "category"]
