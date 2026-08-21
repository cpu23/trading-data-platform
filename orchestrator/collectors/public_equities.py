"""Public equities daily prices and corporate actions (keyless public source).

Fetches daily OHLCV bars plus split/dividend events from the Yahoo Finance
public chart endpoint, whose keyless JSON response contract
(``chart.result[0].timestamp`` + ``indicators.quote`` + ``events``) is
stable and widely consumed.  The default origin is audited public HTTPS;
any custom ``chart_base_url`` is validated against the shared outbound
origin policy (HTTPS, globally routable DNS) before use.

Prices are stored exactly as served (unadjusted, ``metadata.adjusted`` is
explicitly ``false``): this collector never recomputes historical prices
from later actions.  Split/dividend facts arrive in the same response's
``events`` block and are persisted to ``corporate_actions`` with a
deterministic point-in-time ``action_id`` (SHA-256 over source, symbol,
action_type, effective date, and amounts/ratio), so re-collection is an
idempotent no-op while provider amendments produce new rows instead of
rewriting history.

One Collector target cannot cover two tables: ``collect()`` returns the
``market_data`` rows as the primary records and the parsed corporate
actions as one immutable additional write batch on the result, so the
executor persists both tables through a single transaction (all batches
commit or roll back together).  ``collect()`` itself never opens a
database session: a second domain write must not be committed silently
outside the executor's write path.  Both tables are
append-only/immutable (migration 050); the additional batch is
insert-only and writes with ``ON CONFLICT (action_id) DO NOTHING``.

Bounded by configuration: a validated symbol list (capped), a whitelisted
``range``/``interval``, and a bounded per-request timeout.  Empty provider
output for a symbol is valid (bars/actions simply stay missing); malformed
arrays or non-finite values fail that symbol explicitly with
``InvalidSourceData`` and never fabricate values.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from collectors.base import CollectionResult, CollectionWriteBatch
from errors import (
    InvalidSourceData,
    TransientSourceError,
    classify_error,
)
from http_client import ResponseBodyTooLarge, make_request
from http_errors import safe_error_message
from logging_config import get_logger

logger = get_logger("collector.public_equities")

DEFAULT_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
DEFAULT_SCHEDULE = "0 7 * * 1-5"
DEFAULT_USER_AGENT = (
    "trading-data-platform/1.0 (public-equity-daily; free market data collector)"
)
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_RANGE = "1y"
DEFAULT_BOOTSTRAP_RANGE = "3mo"
DEFAULT_INTERVAL = "1d"
DEFAULT_MAX_SYMBOLS = 50
HARD_MAX_SYMBOLS = 400
HARD_MAX_CONCURRENCY = 16
# Bounded download window: whitelisted chart ranges (never "max") and a
# single supported daily interval.
VALID_RANGES = frozenset({"5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y"})
VALID_INTERVALS = frozenset({"1d"})
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 60.0
MAX_RESPONSE_BYTES = 2_000_000
MAX_BARS_PER_SYMBOL = 3_000
MAX_ACTIONS_PER_SYMBOL = 1_000
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,19}$")


def corporate_action_id(
    source: str,
    symbol: str,
    action_type: str,
    effective_date: date,
    amount: float | None = None,
    numerator: float | None = None,
    denominator: float | None = None,
) -> str:
    """Deterministic point-in-time identity for one corporate action row.

    The digest covers the provider facts that define the action (source,
    symbol, type, effective date, amount/ratio) but not the acquisition
    time, so re-collecting the same fact is idempotent while an amended
    amount/ratio/date yields a distinct row instead of an in-place update.
    """
    parts = [
        str(source),
        str(symbol),
        str(action_type),
        str(effective_date),
    ]
    if amount is not None:
        parts.append(f"amount:{amount!r}")
    if numerator is not None and denominator is not None:
        parts.append(f"ratio:{numerator!r}:{denominator!r}")
    canonical = "|".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _collector_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    collectors = config.get("collectors")
    section = (
        collectors.get("public_equities") if isinstance(collectors, Mapping) else None
    )
    return section if isinstance(section, Mapping) else {}


def _finite_nonneg(value: float) -> bool:
    return math.isfinite(value) and value >= 0


class PublicEquitiesCollector:
    source_id = "public_equities"

    # Historical daily bars are insert-once: re-collecting the same bar must
    # never revise the stored row (an upsert would bump ``updated_at`` past
    # every accepted cutoff and rewrite history for replay).  The executor
    # honors this by writing bars with ``ON CONFLICT DO NOTHING``.
    insert_only = True

    # -- configuration -----------------------------------------------------

    def _validated_origin(self, section: Mapping[str, Any]) -> str:
        """Canonical default accepted as-is; custom origins must be HTTPS
        and public (validated against the shared outbound policy)."""
        from contracts.outbound_security import (
            OutboundSecurityError,
            validate_provider_origin,
        )

        configured = str(section.get("chart_base_url") or DEFAULT_CHART_URL).strip()
        if configured.rstrip("/") == DEFAULT_CHART_URL:
            return DEFAULT_CHART_URL
        try:
            return validate_provider_origin(configured).rstrip("/")
        except OutboundSecurityError as exc:
            raise ValueError(f"invalid public_equities chart_base_url ({exc})") from exc

    def _symbols(self, config: Mapping[str, Any]) -> list[str]:
        section = _collector_section(config)
        raw = section.get("symbols", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValueError("public_equities.symbols must be a list of strings")
        configured_cap = section.get("max_symbols", DEFAULT_MAX_SYMBOLS)
        try:
            max_symbols = int(configured_cap)
        except (TypeError, ValueError):
            raise ValueError(
                f"invalid public_equities.max_symbols {configured_cap!r}"
            ) from None
        if not 1 <= max_symbols <= HARD_MAX_SYMBOLS:
            raise ValueError(
                f"public_equities.max_symbols must be between 1 and {HARD_MAX_SYMBOLS}"
            )
        if len(raw) > max_symbols:
            raise ValueError(
                f"public_equities.symbols exceeds the configured limit of "
                f"{max_symbols} symbols"
            )
        symbols: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError("public_equities.symbols must contain strings")
            symbol = item.strip().upper()
            if not _SYMBOL_RE.fullmatch(symbol):
                raise ValueError(f"invalid public_equities symbol {symbol!r}")
            symbols.append(symbol)
        if len(set(symbols)) != len(symbols):
            raise ValueError("public_equities.symbols contains duplicates")
        return symbols

    def _range(self, config: Mapping[str, Any]) -> str:
        section = _collector_section(config)
        value = str(section.get("range") or DEFAULT_RANGE)
        if value not in VALID_RANGES:
            raise ValueError(
                f"public_equities.range must be one of "
                f"{sorted(VALID_RANGES)}, got {value!r}"
            )
        return value

    def _bootstrap_range(self, config: Mapping[str, Any]) -> str:
        section = _collector_section(config)
        value = str(section.get("bootstrap_range") or DEFAULT_BOOTSTRAP_RANGE)
        if value not in VALID_RANGES:
            raise ValueError(
                "public_equities.bootstrap_range must be one of "
                f"{sorted(VALID_RANGES)}, got {value!r}"
            )
        return value

    def _interval(self, config: Mapping[str, Any]) -> str:
        section = _collector_section(config)
        value = str(section.get("interval") or DEFAULT_INTERVAL)
        if value not in VALID_INTERVALS:
            raise ValueError(
                f"public_equities.interval must be one of "
                f"{sorted(VALID_INTERVALS)}, got {value!r}"
            )
        return value

    def _timeout(self, config: Mapping[str, Any]) -> float:
        section = _collector_section(config)
        try:
            timeout = float(section.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            raise ValueError(
                "public_equities.timeout_seconds must be numeric"
            ) from None
        if not MIN_TIMEOUT_SECONDS <= timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(
                f"public_equities.timeout_seconds must be between "
                f"{MIN_TIMEOUT_SECONDS:g} and {MAX_TIMEOUT_SECONDS:g}"
            )
        return timeout

    def _concurrency(self, config: Mapping[str, Any]) -> int:
        section = _collector_section(config)
        try:
            concurrency = int(section.get("max_concurrency", DEFAULT_MAX_CONCURRENCY))
        except (TypeError, ValueError):
            raise ValueError(
                "public_equities.max_concurrency must be an integer"
            ) from None
        if not 1 <= concurrency <= HARD_MAX_CONCURRENCY:
            raise ValueError(
                "public_equities.max_concurrency must be between "
                f"1 and {HARD_MAX_CONCURRENCY}"
            )
        return concurrency

    def _user_agent(self, config: Mapping[str, Any]) -> str:
        section = _collector_section(config)
        value = str(section.get("user_agent") or DEFAULT_USER_AGENT).strip()
        if not value or len(value) > 200:
            raise ValueError("public_equities.user_agent must be a bounded string")
        return value

    @staticmethod
    def _provider_symbol(symbol: str) -> str:
        """Map canonical US class-share dots without rewriting exchange suffixes."""
        base, separator, suffix = symbol.rpartition(".")
        if separator and base and suffix in {"A", "B"}:
            return f"{base}-{suffix}"
        return symbol

    # -- collection --------------------------------------------------------

    def collect(
        self,
        config: Mapping[str, Any],
        correlation_id: str,
        *,
        now: datetime | None = None,
    ) -> CollectionResult:
        """Fetch daily bars + corporate actions for the configured symbols.

        ``now`` pins the acquisition timestamp for deterministic tests;
        production callers use the protocol signature and default to the
        wall clock.  Parsed corporate actions are returned as one
        insert-only additional write batch; the collector never opens a
        database session, so the executor persists bars and actions
        through a single transaction.
        """
        section = _collector_section(config)
        symbols = self._symbols(config)
        chart_url = self._validated_origin(section)
        range_ = self._range(config)
        bootstrap_range = self._bootstrap_range(config)
        interval = self._interval(config)
        timeout = self._timeout(config)
        concurrency = self._concurrency(config)
        user_agent = self._user_agent(config)
        pinned_available_at = now
        bootstrap_symbols = {
            str(symbol or "").strip().upper()
            for symbol in section.get("_bootstrap_symbols", [])
            if _SYMBOL_RE.fullmatch(str(symbol or "").strip().upper())
        }

        records: list[dict] = []
        action_records: list[dict] = []
        errors: list[dict] = []
        successful_series = 0

        def fetch_symbol(
            symbol: str,
        ) -> tuple[
            str,
            list[dict],
            list[dict],
            tuple[str, str, str] | None,
        ]:
            try:
                bars, actions = self._fetch_symbol(
                    chart_url=chart_url,
                    symbol=symbol,
                    range_=bootstrap_range if symbol in bootstrap_symbols else range_,
                    interval=interval,
                    timeout=timeout,
                    user_agent=user_agent,
                    available_at=pinned_available_at,
                    correlation_id=correlation_id,
                )
                return symbol, bars, actions, None
            except ResponseBodyTooLarge as exc:
                return (
                    symbol,
                    [],
                    [],
                    (
                        "response_oversize",
                        InvalidSourceData.error_class,
                        type(exc).__name__,
                    ),
                )
            except httpx.HTTPError as exc:
                return (
                    symbol,
                    [],
                    [],
                    (
                        "request_failed",
                        TransientSourceError.error_class,
                        type(exc).__name__,
                    ),
                )
            except Exception as exc:
                policy = classify_error(exc)
                return (
                    symbol,
                    [],
                    [],
                    ("parse_failed", policy.error_class, type(exc).__name__),
                )

        if symbols:
            with ThreadPoolExecutor(
                max_workers=min(concurrency, len(symbols)),
                thread_name_prefix="public-equities",
            ) as executor:
                results = executor.map(fetch_symbol, symbols)
                for symbol, bars, actions, failure in results:
                    if failure is not None:
                        code, error_class, exception_type = failure
                        errors.append(
                            {
                                "symbol": symbol,
                                "stage": "chart",
                                "code": code,
                                "exception_type": exception_type,
                                "error_class": error_class,
                            }
                        )
                        logger.warning(
                            f"public_equities_symbol_{code}",
                            action="collect_symbol",
                            symbol=symbol,
                            error_type=exception_type,
                            correlation_id=correlation_id,
                        )
                        continue
                    records.extend(bars)
                    action_records.extend(actions)
                    successful_series += 1
        # Never a second domain write: actions are declared as an immutable
        # additional batch the executor persists in the same transaction as
        # the bars.
        additional_writes = []
        if action_records:
            additional_writes.append(
                CollectionWriteBatch(
                    table_name="corporate_actions",
                    records=action_records,
                    conflict_columns=["action_id"],
                    insert_only=True,
                )
            )
        metrics = {
            "api_calls_made": len(symbols),
            "bars_fetched": len(records),
            "corporate_actions_fetched": len(action_records),
        }
        return CollectionResult(
            records=records,
            additional_writes=additional_writes,
            errors=errors,
            total_series=len(symbols),
            successful_series=successful_series,
            metrics=metrics,
        )

    def _fetch_symbol(
        self,
        *,
        chart_url: str,
        symbol: str,
        range_: str,
        interval: str,
        timeout: float,
        user_agent: str,
        available_at: datetime | None,
        correlation_id: str,
    ) -> tuple[list[dict], list[dict]]:
        provider_symbol = self._provider_symbol(symbol)
        source_reference = f"{chart_url}/{provider_symbol}"
        response = make_request(
            method="GET",
            url=source_reference,
            params={"range": range_, "interval": interval, "events": "div,split"},
            headers={"User-Agent": user_agent},
            timeout=timeout,
            max_retries=2,
            correlation_id=correlation_id,
            follow_redirects=True,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        response.raise_for_status()
        # Defense in depth: make_request enforces the cap while streaming, but
        # a response that did not pass through the bounded streamer must still
        # be rejected before any JSON parsing.
        body = response.content
        if isinstance(body, (bytes, bytearray)) and len(body) > MAX_RESPONSE_BYTES:
            raise ResponseBodyTooLarge(
                f"public equity chart exceeds {MAX_RESPONSE_BYTES} bytes",
                request=response.request,
            )
        payload = response.json()
        observed_available_at = available_at or datetime.now(UTC)
        return self._parse_symbol_payload(
            symbol,
            range_,
            interval,
            payload,
            observed_available_at,
            source_reference=source_reference,
        )

    def _parse_symbol_payload(
        self,
        symbol: str,
        range_: str,
        interval: str,
        payload: Any,
        available_at: datetime,
        *,
        source_reference: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        chart = payload.get("chart") if isinstance(payload, dict) else None
        if not isinstance(chart, dict):
            raise InvalidSourceData("chart payload is not an object")
        if chart.get("error"):
            error = chart["error"]
            code = error.get("code") if isinstance(error, dict) else "provider_error"
            raise InvalidSourceData(f"chart error {code}")
        result = chart.get("result")
        if result is None:
            raise InvalidSourceData("chart result is missing")
        if not isinstance(result, list):
            raise InvalidSourceData("chart result is not a list")
        if not result:
            # Empty bounded output is valid: no bars, no actions.
            return [], []
        if len(result) != 1:
            raise InvalidSourceData("chart result must contain exactly one symbol")
        item = result[0]
        if not isinstance(item, dict):
            raise InvalidSourceData("chart result entry is not an object")

        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        provider_symbol = str(meta.get("symbol") or "").strip().upper()
        expected_provider_symbol = self._provider_symbol(symbol).upper()
        if not provider_symbol or provider_symbol != expected_provider_symbol:
            raise InvalidSourceData("chart response symbol does not match request")
        timestamps = item.get("timestamp")
        if not isinstance(timestamps, list):
            raise InvalidSourceData("chart timestamp array is missing or malformed")
        if len(timestamps) > MAX_BARS_PER_SYMBOL:
            raise InvalidSourceData("chart timestamp array exceeds row bound")
        indicators = item.get("indicators")
        quote = (
            indicators.get("quote")[0]
            if isinstance(indicators, dict)
            and isinstance(indicators.get("quote"), list)
            and indicators.get("quote")
            else None
        )
        if not isinstance(quote, dict):
            raise InvalidSourceData("chart quote indicators are missing or malformed")

        arrays: dict[str, list[Any]] = {}
        for field in ("open", "high", "low", "close", "volume"):
            values = quote.get(field)
            if not isinstance(values, list):
                raise InvalidSourceData(f"chart {field} array is missing")
            if len(values) != len(timestamps):
                raise InvalidSourceData(
                    f"chart {field} array length does not match timestamps"
                )
            arrays[field] = values

        source_timestamp = self._provider_timestamp(meta.get("regularMarketTime"))
        if source_timestamp is not None and source_timestamp > available_at + timedelta(
            minutes=5
        ):
            raise InvalidSourceData("chart market timestamp is in the future")
        bar_metadata = {
            "adjusted": False,
            "interval": interval,
            "range": range_,
            "provider_symbol": provider_symbol,
            "source_reference": source_reference,
            "currency": meta.get("currency"),
            "exchange_name": meta.get("exchangeName"),
            "source_timestamp": source_timestamp.isoformat()
            if source_timestamp is not None
            else None,
            "available_at": available_at.isoformat(),
        }

        bars: list[dict] = []
        for index, raw_timestamp in enumerate(timestamps):
            if raw_timestamp is None:
                # Missing bars remain missing; nothing is fabricated.
                continue
            close = self._optional_float(arrays["close"][index])
            if close is None:
                continue
            try:
                bar_time = datetime.fromtimestamp(raw_timestamp, tz=UTC)
            except (TypeError, ValueError, OverflowError):
                raise InvalidSourceData(
                    f"invalid bar timestamp at index {index}"
                ) from None
            if bar_time > available_at + timedelta(days=1):
                raise InvalidSourceData("chart bar timestamp is in the future")
            open_ = self._optional_float(arrays["open"][index])
            high = self._optional_float(arrays["high"][index])
            low = self._optional_float(arrays["low"][index])
            volume = self._optional_float(arrays["volume"][index])
            for name, value in (
                ("open", open_),
                ("high", high),
                ("low", low),
                ("close", close),
                ("volume", volume),
            ):
                if value is not None and not _finite_nonneg(value):
                    raise InvalidSourceData(
                        f"non-finite or negative {name} at index {index}"
                    )
            bars.append(
                {
                    "symbol": symbol,
                    "timeframe": interval,
                    "timestamp": bar_time,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "source": self.source_id,
                    "metadata": bar_metadata,
                }
            )

        actions = self._parse_events(symbol, item.get("events"), available_at)
        return bars, actions

    def _parse_events(
        self,
        symbol: str,
        events: Any,
        available_at: datetime,
    ) -> list[dict]:
        if events is None:
            return []
        if not isinstance(events, dict):
            raise InvalidSourceData("chart events is not an object")
        total_actions = sum(
            len(bucket) for bucket in events.values() if isinstance(bucket, dict)
        )
        if total_actions > MAX_ACTIONS_PER_SYMBOL:
            raise InvalidSourceData("chart corporate actions exceed row bound")
        actions: list[dict] = []
        for action_type, key in (("dividend", "dividends"), ("split", "splits")):
            bucket = events.get(key)
            if bucket is None:
                continue
            if not isinstance(bucket, dict):
                raise InvalidSourceData(f"chart {key} events is not an object")
            for epoch_key, event in bucket.items():
                actions.append(
                    self._parse_event(
                        symbol, action_type, epoch_key, event, available_at
                    )
                )
        return actions

    def _parse_event(
        self,
        symbol: str,
        action_type: str,
        epoch_key: Any,
        event: Any,
        available_at: datetime,
    ) -> dict:
        if not isinstance(event, dict):
            raise InvalidSourceData(f"malformed {action_type} event")
        try:
            epoch = int(epoch_key)
            event_time = datetime.fromtimestamp(epoch, tz=UTC)
        except (TypeError, ValueError, OverflowError):
            raise InvalidSourceData(
                f"malformed {action_type} event timestamp"
            ) from None
        effective_date = event_time.date()
        source_timestamp = event_time
        if action_type == "dividend":
            amount = self._required_positive_float(
                event.get("amount"), "dividend amount"
            )
            action_id = corporate_action_id(
                self.source_id, symbol, action_type, effective_date, amount=amount
            )
            return {
                "action_id": action_id,
                "symbol": symbol,
                "action_type": action_type,
                "effective_date": effective_date,
                "source": self.source_id,
                "source_timestamp": source_timestamp,
                "available_at": available_at,
                "amount": amount,
                "ratio_numerator": None,
                "ratio_denominator": None,
                "description": None,
                "metadata": {"provider_event_key": str(epoch_key)},
            }
        numerator = self._required_positive_float(
            event.get("numerator"), "split numerator"
        )
        denominator = self._required_positive_float(
            event.get("denominator"), "split denominator"
        )
        split_ratio = event.get("splitRatio")
        description = (
            str(split_ratio)
            if isinstance(split_ratio, str) and split_ratio.strip()
            else f"{numerator:g}:{denominator:g}"
        )
        action_id = corporate_action_id(
            self.source_id,
            symbol,
            action_type,
            effective_date,
            numerator=numerator,
            denominator=denominator,
        )
        return {
            "action_id": action_id,
            "symbol": symbol,
            "action_type": action_type,
            "effective_date": effective_date,
            "source": self.source_id,
            "source_timestamp": source_timestamp,
            "available_at": available_at,
            "amount": None,
            "ratio_numerator": numerator,
            "ratio_denominator": denominator,
            "description": description,
            "metadata": {"provider_event_key": str(epoch_key)},
        }

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        """Parse a nullable provider numeric; non-null garbage is malformed."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise InvalidSourceData("non-numeric bar value") from None

    @staticmethod
    def _required_positive_float(value: Any, label: str) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise InvalidSourceData(f"non-numeric {label}") from None
        if not (math.isfinite(parsed) and parsed > 0):
            raise InvalidSourceData(f"non-finite or non-positive {label}")
        return parsed

    @staticmethod
    def _provider_timestamp(value: Any) -> datetime | None:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (TypeError, ValueError, OverflowError):
            return None

    # -- Collector protocol -------------------------------------------------

    def get_schedule(self, config: dict) -> str:
        return _collector_section(config).get("schedule", DEFAULT_SCHEDULE)

    def health_check(self, config: dict) -> dict:
        section = _collector_section(config)
        start_ms = time.monotonic() * 1000
        try:
            # Validate configuration before any network access.
            symbols = self._symbols(config)
            chart_url = self._validated_origin(section)
            range_ = self._range(config)
            self._bootstrap_range(config)
            interval = self._interval(config)
            timeout = self._timeout(config)
            self._concurrency(config)
            user_agent = self._user_agent(config)
        except ValueError as exc:
            return {"healthy": False, "message": str(exc), "latency_ms": 0}
        if not symbols:
            return {
                "healthy": False,
                "message": "public_equities has no configured symbols",
                "latency_ms": 0,
            }
        try:
            response = make_request(
                method="GET",
                url=f"{chart_url}/{self._provider_symbol(symbols[0])}",
                params={"range": range_, "interval": interval, "events": "div,split"},
                headers={"User-Agent": user_agent},
                timeout=timeout,
                max_retries=1,
                correlation_id="health-check",
                follow_redirects=True,
            )
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            if response.status_code != 200:
                return {
                    "healthy": False,
                    "message": f"public chart API returned status {response.status_code}",
                    "latency_ms": latency_ms,
                }
            payload = response.json()
            chart = payload.get("chart") if isinstance(payload, dict) else None
            if isinstance(chart, dict) and not chart.get("error"):
                return {
                    "healthy": True,
                    "message": "public chart API reachable",
                    "latency_ms": latency_ms,
                }
            return {
                "healthy": False,
                "message": "public chart API returned an error payload",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = int(time.monotonic() * 1000 - start_ms)
            return {
                "healthy": False,
                "message": f"public chart API unreachable: {safe_error_message(exc, provider='public_equities')}",
                "latency_ms": latency_ms,
            }

    def get_target_table(self) -> str:
        return "market_data"

    def get_conflict_columns(self) -> list[str]:
        return ["symbol", "timeframe", "timestamp"]
