import json
import math
import threading
import time
from datetime import datetime, timezone

import httpx

from collectors.oanda import OandaCollector
from logging_config import get_logger

logger = get_logger("price_stream")


class QuoteStream:
    def __init__(self):
        self.quotes: dict[str, dict] = {}
        self.state = {"status": "stopped", "last_heartbeat": None, "error": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, config: dict) -> None:
        if self._thread and self._thread.is_alive():
            return
        oanda_config = config.get("collectors", {}).get("oanda", {})
        demo = config.get("demo", {}).get("enabled", False)
        if not demo and not oanda_config.get("stream_enabled", False):
            return
        target = self._run_demo if demo else self._run_oanda
        self._thread = threading.Thread(target=target, args=(config,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        return {"quotes": list(self.quotes.values()), "stream": dict(self.state)}

    def _update(self, symbol: str, price: float, observed_at: str | None = None) -> None:
        self.quotes[symbol] = {
            "symbol": symbol,
            "price": price,
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        }

    def _run_demo(self, config: dict) -> None:
        bases = {"EURUSD": 1.0875, "AUDJPY": 98.42, "USDJPY": 149.35, "SP500": 5325.0,
                 "XAUUSD": 2388.0, "XPTUSD": 1012.0, "GER40": 18650.0, "UK100": 8320.0}
        self.state["status"] = "simulated"
        tick = 0
        while not self._stop.wait(2):
            tick += 1
            for index, (symbol, base) in enumerate(bases.items()):
                self._update(symbol, base * (1 + math.sin(tick / 7 + index) * 0.0004))
            self.state["last_heartbeat"] = datetime.now(timezone.utc).isoformat()

    def _run_oanda(self, config: dict) -> None:
        oanda_config = config.get("collectors", {}).get("oanda", {})
        collector = OandaCollector()
        api_key = oanda_config.get("api_key", "")
        base_url = collector._get_base_url(oanda_config).replace("api-", "stream-")
        backoff = 1
        while not self._stop.is_set():
            try:
                api_base = collector._get_base_url(oanda_config)
                account_id = collector._get_account_id(api_base, api_key, oanda_config, "price-stream")
                instruments = collector._filter_supported_instruments(
                    api_base, api_key, account_id,
                    [item for item in oanda_config.get("instruments", []) if item.get("enabled", True)],
                    "price-stream",
                )
                names = ",".join(item["oanda_instrument"] for item in instruments)
                symbol_map = {item["oanda_instrument"]: item["symbol"] for item in instruments}
                headers = {"Authorization": f"Bearer {api_key}", "Accept-Datetime-Format": "RFC3339"}
                self.state.update(status="connected", error=None)
                with httpx.Client(timeout=None, follow_redirects=True) as client:
                    with client.stream(
                        "GET",
                        f"{base_url.rstrip('/')}/v3/accounts/{account_id}/pricing/stream",
                        params={"instruments": names},
                        headers=headers,
                    ) as response:
                        response.raise_for_status()
                        backoff = 1
                        for line in response.iter_lines():
                            if self._stop.is_set():
                                return
                            if not line:
                                continue
                            payload = json.loads(line)
                            if payload.get("type") == "HEARTBEAT":
                                self.state["last_heartbeat"] = payload.get("time")
                                continue
                            symbol = symbol_map.get(payload.get("instrument"))
                            price = collector._extract_mid_price(payload)
                            if symbol and price is not None:
                                self._update(symbol, price, payload.get("time"))
            except Exception as exc:
                self.state.update(status="reconnecting", error=str(exc))
                logger.warning("price_stream_reconnecting", error=str(exc), backoff=backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)


quote_stream = QuoteStream()
