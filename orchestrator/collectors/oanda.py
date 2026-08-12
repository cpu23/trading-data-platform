import time
from datetime import UTC, datetime

from http_client import make_request
from logging_config import get_logger

logger = get_logger("collector.oanda")

DEFAULT_BASE_URLS = {
    "live": "https://api-fxtrade.oanda.com",
    "practice": "https://api-fxpractice.oanda.com",
}


class OandaCollector:
    source_id = "oanda"

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        oanda_config = config.get("collectors", {}).get("oanda", {})
        api_key = oanda_config.get("api_key", "")
        if not api_key:
            raise ValueError("OANDA_API_KEY is not set")

        base_url = self._get_base_url(oanda_config)
        snapshot_timeframe = oanda_config.get("snapshot_timeframe", "PRICE")
        instruments = [
            item
            for item in oanda_config.get("instruments", [])
            if item.get("enabled", True)
        ]

        account_id = self._get_account_id(
            base_url, api_key, oanda_config, correlation_id
        )
        instruments = self._filter_supported_instruments(
            base_url, api_key, account_id, instruments, correlation_id
        )
        records = self._collect_prices(
            base_url=base_url,
            api_key=api_key,
            account_id=account_id,
            instruments=instruments,
            snapshot_timeframe=snapshot_timeframe,
            correlation_id=correlation_id,
        )

        logger.info(
            "oanda_prices_collected",
            action="collect_prices",
            records_fetched=len(records),
            correlation_id=correlation_id,
        )
        return records

    def _get_account_id(
        self,
        base_url: str,
        api_key: str,
        oanda_config: dict,
        correlation_id: str,
    ) -> str:
        configured = oanda_config.get("account_id")
        if configured:
            return configured

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept-Datetime-Format": "RFC3339",
        }
        response = make_request(
            method="GET",
            url=f"{base_url.rstrip('/')}/v3/accounts",
            headers=headers,
            timeout=15.0,
            max_retries=1,
            correlation_id=correlation_id,
            follow_redirects=True,
        )
        response.raise_for_status()
        accounts = response.json().get("accounts", [])
        if not accounts:
            raise RuntimeError("OANDA returned no accounts for this token")
        account_id = accounts[0].get("id")
        if not account_id:
            raise RuntimeError("OANDA account response did not include an account id")
        return account_id

    def _filter_supported_instruments(
        self,
        base_url: str,
        api_key: str,
        account_id: str,
        instruments: list[dict],
        correlation_id: str,
    ) -> list[dict]:
        if not instruments:
            return []
        response = make_request(
            method="GET",
            url=f"{base_url.rstrip('/')}/v3/accounts/{account_id}/instruments",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            max_retries=1,
            correlation_id=correlation_id,
            follow_redirects=True,
        )
        response.raise_for_status()
        supported = {
            item.get("name")
            for item in response.json().get("instruments", [])
            if item.get("name")
        }
        filtered = [
            item for item in instruments if item.get("oanda_instrument") in supported
        ]
        skipped = [
            item.get("oanda_instrument")
            for item in instruments
            if item.get("oanda_instrument") not in supported
        ]
        if skipped:
            logger.warning(
                "oanda_unsupported_instruments_skipped",
                instruments=skipped,
                correlation_id=correlation_id,
            )
        return filtered

    def _collect_prices(
        self,
        base_url: str,
        api_key: str,
        account_id: str,
        instruments: list[dict],
        snapshot_timeframe: str,
        correlation_id: str,
    ) -> list[dict]:
        instrument_map = {
            item["oanda_instrument"]: item["symbol"]
            for item in instruments
            if item.get("oanda_instrument")
        }
        if not instrument_map:
            return []

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept-Datetime-Format": "RFC3339",
        }
        response = make_request(
            method="GET",
            url=f"{base_url.rstrip('/')}/v3/accounts/{account_id}/pricing",
            params={"instruments": ",".join(instrument_map.keys())},
            headers=headers,
            timeout=15.0,
            max_retries=1,
            correlation_id=correlation_id,
            follow_redirects=True,
        )
        response.raise_for_status()

        payload = response.json()
        records = []
        for price in payload.get("prices", []):
            oanda_name = price.get("instrument")
            symbol = instrument_map.get(oanda_name)
            if not symbol:
                continue
            mid = self._extract_mid_price(price)
            if mid is None:
                logger.warning(
                    "oanda_price_parse_skipped",
                    action="parse_price",
                    symbol=symbol,
                    oanda_instrument=oanda_name,
                    reason="missing bid/ask price",
                    correlation_id=correlation_id,
                )
                continue
            try:
                records.append(
                    {
                        "symbol": symbol,
                        "timeframe": snapshot_timeframe,
                        "timestamp": self._parse_oanda_time(price["time"]),
                        "open": mid,
                        "high": mid,
                        "low": mid,
                        "close": mid,
                        "volume": None,
                        "source": self.source_id,
                    }
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "oanda_price_parse_skipped",
                    action="parse_price",
                    symbol=symbol,
                    oanda_instrument=oanda_name,
                    error=str(exc),
                    correlation_id=correlation_id,
                )

        return records

    def _extract_mid_price(self, price: dict) -> float | None:
        bid = self._first_bucket_price(price.get("bids"))
        ask = self._first_bucket_price(price.get("asks"))
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        if bid is not None:
            return bid
        if ask is not None:
            return ask
        closeout_bid = price.get("closeoutBid")
        closeout_ask = price.get("closeoutAsk")
        try:
            if closeout_bid is not None and closeout_ask is not None:
                return (float(closeout_bid) + float(closeout_ask)) / 2
        except (TypeError, ValueError):
            return None
        return None

    def _first_bucket_price(self, buckets: list[dict] | None) -> float | None:
        if not buckets:
            return None
        try:
            return float(buckets[0]["price"])
        except (KeyError, TypeError, ValueError):
            return None

    def _get_base_url(self, oanda_config: dict) -> str:
        if oanda_config.get("base_url"):
            return self._validated_origin(
                str(oanda_config["base_url"]), oanda_config
            )
        environment = oanda_config.get("environment", "live")
        return DEFAULT_BASE_URLS.get(environment, DEFAULT_BASE_URLS["live"])

    @staticmethod
    def _validated_origin(base_url: str, oanda_config: dict) -> str:
        """Canonical OANDA origins are fixed; custom origins must be HTTPS
        and public (validated against the shared policy)."""
        from contracts.outbound_security import (
            OutboundSecurityError,
            validate_provider_origin,
        )

        if base_url in set(DEFAULT_BASE_URLS.values()):
            return base_url
        try:
            return validate_provider_origin(base_url)
        except OutboundSecurityError as exc:
            raise ValueError(f"invalid OANDA base_url ({exc})") from exc

    def _parse_oanda_time(self, value: str) -> datetime:
        raw = value.replace("Z", "+00:00")
        if "." in raw:
            prefix, suffix = raw.split(".", 1)
            fraction, offset = suffix[:6], suffix[6:]
            if "+" in suffix:
                fraction, offset = suffix.split("+", 1)
                offset = f"+{offset}"
            elif "-" in suffix:
                fraction, offset = suffix.split("-", 1)
                offset = f"-{offset}"
            fraction = fraction[:6].ljust(6, "0")
            raw = f"{prefix}.{fraction}{offset}"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def get_schedule(self, config: dict) -> str:
        return config["collectors"]["oanda"]["schedule"]

    def health_check(self, config: dict) -> dict:
        oanda_config = config.get("collectors", {}).get("oanda", {})
        api_key = oanda_config.get("api_key", "")
        start_ms = time.monotonic() * 1000

        if not api_key:
            return {
                "healthy": False,
                "message": "OANDA_API_KEY is not set",
                "latency_ms": 0,
            }

        try:
            response = make_request(
                method="GET",
                url=f"{self._get_base_url(oanda_config).rstrip('/')}/v3/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15.0,
                max_retries=1,
                correlation_id="health-check",
                follow_redirects=True,
            )
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            if response.status_code == 200:
                return {
                    "healthy": True,
                    "message": "OANDA API reachable",
                    "latency_ms": latency_ms,
                }
            return {
                "healthy": False,
                "message": f"OANDA API returned status {response.status_code}",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            return {
                "healthy": False,
                "message": f"OANDA API unreachable: {exc}",
                "latency_ms": latency_ms,
            }

    def get_target_table(self) -> str:
        return "market_data"

    def get_conflict_columns(self) -> list[str]:
        return ["symbol", "timeframe", "timestamp"]
