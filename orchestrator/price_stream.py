import json
import math
import threading
from datetime import UTC, datetime

import httpx
from sqlalchemy import text

from collectors.oanda import OandaCollector
from db import get_session
from http_client import PublicOnlyHTTPTransport
from logging_config import get_logger
from role_heartbeat import fresh_role_heartbeats

logger = get_logger("price_stream")


def _upsert_quote(config: dict, symbol: str, price: float, observed_at: str) -> None:
    """Persist one quote observation; the API reads quotes from the DB."""
    try:
        with get_session(config) as session:
            session.execute(
                text(
                    "INSERT INTO quote_state (symbol, price, observed_at, updated_at) "
                    "VALUES (:symbol, :price, :observed_at, :now) "
                    "ON CONFLICT (symbol) DO UPDATE SET price = EXCLUDED.price, "
                    "observed_at = EXCLUDED.observed_at, updated_at = EXCLUDED.updated_at"
                ),
                {
                    "symbol": symbol,
                    "price": float(price),
                    "observed_at": observed_at,
                    "now": datetime.now(UTC).isoformat(),
                },
            )
    except Exception as exc:
        logger.warning("quote_persist_failed", symbol=symbol, error_type=type(exc).__name__)


def db_snapshot(config: dict) -> dict:
    """Durable quote snapshot for the HTTP API role (reads quote_state)."""
    quotes: list[dict] = []
    try:
        with get_session(config) as session:
            rows = session.execute(
                text(
                    "SELECT symbol, price, observed_at, updated_at "
                    "FROM quote_state ORDER BY symbol"
                )
            ).mappings()
            for row in rows:
                observed = row["observed_at"]
                quotes.append(
                    {
                        "symbol": row["symbol"],
                        "price": row["price"],
                        "observed_at": (
                            observed.isoformat()
                            if hasattr(observed, "isoformat")
                            else observed
                        ),
                        "updated_at": (
                            row["updated_at"].isoformat()
                            if hasattr(row["updated_at"], "isoformat")
                            else row["updated_at"]
                        ),
                    }
                )
    except Exception as exc:
        logger.warning(
            "quote_snapshot_unavailable", error_type=type(exc).__name__
        )
    stream = {"status": "stopped", "last_heartbeat": None, "error": None}
    try:
        fresh = fresh_role_heartbeats(config, "quotes")
    except Exception:
        fresh = []
    # Prefer any fresh healthy (connected/simulated) instance; a newer
    # stopped/disabled sibling must not mask a running replica's status.
    healthy = [
        heartbeat
        for heartbeat in fresh
        if heartbeat.get("status") in ("connected", "simulated")
    ]
    preferred = healthy[0] if healthy else (fresh[0] if fresh else None)
    if preferred is not None:
        stream["status"] = preferred.get("status", "stopped")
        last = preferred.get("last_heartbeat_at")
        stream["last_heartbeat"] = (
            last.isoformat() if hasattr(last, "isoformat") else last
        )
        detail = preferred.get("detail") or {}
        stream["error"] = detail.get("error")
        stream["instances"] = len(fresh)
        stream["healthy_instances"] = len(healthy)
    return {"quotes": quotes, "stream": stream}


def _stream_client(oanda_config: dict) -> httpx.Client:
    """OANDA streaming client built on the resolve-and-pin public transport:
    the long-lived stream cannot be rebound to a private host. Redirects are
    disabled outright: the bearer-token credential must never follow a
    Location, so a 3xx is treated as a failure by the caller."""
    return httpx.Client(
        transport=PublicOnlyHTTPTransport(),
        timeout=None,
        follow_redirects=False,
    )


class QuoteStream:
    def __init__(self):
        self.quotes: dict[str, dict] = {}
        self.state = {"status": "stopped", "last_heartbeat": None, "error": None}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._config: dict | None = None

    def start(self, config: dict) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._config = config
        oanda_config = config.get("collectors", {}).get("oanda", {})
        demo = config.get("demo", {}).get("enabled", False)
        if demo:
            target = self._run_demo
        elif oanda_config.get("enabled", True) and oanda_config.get(
            "stream_enabled", False
        ):
            target = self._run_oanda
        else:
            self.state.update(
                status="disabled",
                error="OANDA live stream is not enabled",
            )
            logger.info("no_live_price_stream_configured")
            return
        self._thread = threading.Thread(target=target, args=(config,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        return {"quotes": list(self.quotes.values()), "stream": dict(self.state)}

    def _update(
        self, symbol: str, price: float, observed_at: str | None = None
    ) -> None:
        observed = observed_at or datetime.now(UTC).isoformat()
        self.quotes[symbol] = {
            "symbol": symbol,
            "price": price,
            "observed_at": observed,
        }
        if self._config is not None:
            _upsert_quote(self._config, symbol, price, observed)

    def _run_demo(self, config: dict) -> None:
        bases = {
            "EURUSD": 1.0875,
            "AUDJPY": 98.42,
            "USDJPY": 149.35,
            "SP500": 5325.0,
            "XAUUSD": 2388.0,
            "XPTUSD": 1012.0,
            "GER40": 18650.0,
            "UK100": 8320.0,
        }
        self.state["status"] = "simulated"
        tick = 0
        while not self._stop.wait(2):
            tick += 1
            for index, (symbol, base) in enumerate(bases.items()):
                self._update(symbol, base * (1 + math.sin(tick / 7 + index) * 0.0004))
            self.state["last_heartbeat"] = datetime.now(UTC).isoformat()

    def _run_oanda(self, config: dict) -> None:
        oanda_config = config.get("collectors", {}).get("oanda", {})
        collector = OandaCollector()
        api_key = oanda_config.get("api_key", "")
        base_url = collector._get_base_url(oanda_config).replace("api-", "stream-")
        backoff = 1
        while not self._stop.is_set():
            try:
                api_base = collector._get_base_url(oanda_config)
                account_id = collector._get_account_id(
                    api_base, api_key, oanda_config, "price-stream"
                )
                instruments = collector._filter_supported_instruments(
                    api_base,
                    api_key,
                    account_id,
                    [
                        item
                        for item in oanda_config.get("instruments", [])
                        if item.get("enabled", True)
                    ],
                    "price-stream",
                )
                names = ",".join(item["oanda_instrument"] for item in instruments)
                symbol_map = {
                    item["oanda_instrument"]: item["symbol"] for item in instruments
                }
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Accept-Datetime-Format": "RFC3339",
                }
                self.state.update(status="connected", error=None)
                with _stream_client(oanda_config) as client:
                    with client.stream(
                        "GET",
                        f"{base_url}/v3/accounts/{account_id}/pricing/stream",
                        params={"instruments": names},
                        headers=headers,
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            # Redirects are rejected outright: the bearer
                            # credential must never follow a Location, and no
                            # second request is ever sent for a 3xx.
                            raise RuntimeError(
                                "price stream redirected "
                                f"(HTTP {response.status_code}); redirects are rejected"
                            )
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
                logger.warning(
                    "price_stream_reconnecting", error=str(exc), backoff=backoff
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)


quote_stream = QuoteStream()
