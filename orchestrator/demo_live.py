"""Credential-free deterministic price publisher for demo mode."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from config_loader import load_config
from logging_config import get_logger, setup_logging
from sqlalchemy import text

from db import get_session

logger = get_logger("demo_live")

DEFAULT_INTERVAL_SECONDS = 10.0
DEMO_PRICES = (
    ("EURUSD", 1.0875, 0.0002),
    ("AUDJPY", 98.42, 0.03),
    ("USDJPY", 149.35, 0.04),
    ("XAUUSD", 2384.2, 0.8),
)
_INSERT_PRICE = text(
    """INSERT INTO market_data
       (symbol, timeframe, timestamp, open, high, low, close, source)
       VALUES (:symbol, 'PRICE', :timestamp, :price, :price, :price, :price, 'demo-live')
       ON CONFLICT (symbol, timeframe, timestamp) DO NOTHING"""
)


def _demo_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("demo", {}).get("enabled")) and os.environ.get(
        "DEMO_MODE", ""
    ).lower() in {"1", "true", "yes"}


def publish_demo_tick(
    config: dict[str, Any],
    tick: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Publish one deterministic bounded demo update in a single transaction."""
    if not _demo_enabled(config):
        raise RuntimeError("demo live publisher requires DEMO_MODE=true")
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    phase = (int(tick) % 5) - 2
    version = max(1, int(observed_at.timestamp() * 1000))
    with get_session(config) as session:
        for symbol, base, step in DEMO_PRICES:
            price = round(base + phase * step, 5)
            session.execute(
                _INSERT_PRICE,
                {"symbol": symbol, "timestamp": observed_at, "price": price},
            )
        session.commit()
    return {
        "tick": int(tick),
        "observed_at": observed_at.isoformat(),
        "price_rows": len(DEMO_PRICES),
        "section_version": version,
        "event_id": None,
    }


def run_demo_live(config: dict[str, Any], stop_event: Any) -> None:
    """Publish deterministic demo ticks until the combined worker stops."""
    if not _demo_enabled(config):
        return
    interval = max(
        2.0,
        min(
            60.0,
            float(
                os.environ.get("DEMO_LIVE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
            ),
        ),
    )
    tick = 0
    logger.info("demo_live_started", interval_seconds=interval)
    while not stop_event.is_set():
        try:
            result = publish_demo_tick(config, tick)
            logger.info("demo_live_tick", **result)
            tick += 1
        except Exception as exc:
            logger.warning("demo_live_tick_failed", error_type=type(exc).__name__)
        stop_event.wait(interval)


def main() -> None:
    import threading

    config = load_config()
    setup_logging(level=config.get("logging", {}).get("level", "INFO"))
    if not _demo_enabled(config):
        raise SystemExit("demo live publisher refuses to run outside DEMO_MODE=true")
    run_demo_live(config, threading.Event())


if __name__ == "__main__":
    main()
