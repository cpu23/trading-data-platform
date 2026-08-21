"""Bounded no-key Cboe delayed options-chain collector.

Source: Cboe's public delayed-quotes API
(``https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json``),
an undocumented but public, no-key endpoint serving exchange delayed quotes
(15-minute delay during market hours). One request returns the full listed
chain for a symbol across expirations; each contract carries bid/ask/last,
volume, open interest, IV and greeks.

Design rules:

- The endpoint URL, symbol list and every bound are operator-configurable;
  the default origin is validated through the shared outbound-origin policy.
- Output is bounded: symbols, contracts per symbol, distinct expirations,
  response bytes and the request rate are all capped by configuration.
- ``captured_at`` is the platform acquisition/availability time (the
  point-in-time identity of the snapshot); ``source_timestamp`` is the
  provider's quote time, assumed to be exchange local time
  (``source_timezone``, default ``America/Chicago``) and stored converted to
  UTC. Both times, plus the raw source time, are exposed per record.
- Malformed payloads fail explicitly (``InvalidSourceData``) and abort the
  run; transient per-symbol network failures are isolated and reported as a
  partial failure, and the run fails when every requested symbol failed.
  Impossible per-contract quotes (crossed markets, negative or non-finite
  values, malformed OCC symbols, root mismatches) reject that contract and
  are counted, never silently coerced. Missing values are preserved as NULL.
- A structurally valid chain whose contracts all reject is treated as
  malformed data and fails; a genuinely empty chain is a valid empty result.
- One immutable analytics feature row per analyzed snapshot
  (``option_snapshot_features``, insert-only) is returned as an additional
  write batch alongside the chain rows: the pure analyzer runs over exactly
  the bounded validated contracts that are persisted, never over rejected or
  truncated-away rows, and explicit insufficient-data/insufficient-history
  states are preserved verbatim.
"""

from __future__ import annotations

import math
import time as _time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from collectors.base import CollectionResult, CollectionWriteBatch
from errors import InvalidSourceData, TransientSourceError
from http_client import ResponseBodyTooLarge, make_request
from logging_config import get_logger
from options_analytics import analyze_chain
from provider_origins import validate_configured_origin

logger = get_logger("collector.cboe_options")

SOURCE_ID = "cboe_options"
#: Analytics contract version stored with every feature row; bump when the
#: analyzer output shape changes so consumers can distinguish generations.
FEATURE_VERSION = "option-analytics-v1"
FEATURE_TABLE = "option_snapshot_features"
DEFAULT_BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options"
DEFAULT_SOURCE_TIMEZONE = "America/Chicago"
DEFAULT_USER_AGENT = (
    "trading-data-platform/0.1 (delayed-options collector; operator configured)"
)
DEFAULT_DELAY_MINUTES = 15

#: Runtime caps applied on top of operator configuration. Symbols beyond the
#: cap are deterministically dropped (first N in configured order); chains are
#: kept to the nearest-term expirations and a bounded contract count.
DEFAULT_BOUNDS = {
    "max_symbols": 25,
    "max_contracts_per_symbol": 20_000,
    "max_expiries": 40,
    "max_response_bytes": 30_000_000,
    "rate_delay_seconds": 1.0,
    "request_deadline_seconds": 60.0,
}

_SOURCE_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S")

#: Contract symbols are OCC-style: ROOT + YYMMDD + C|P + 8-digit strike*1000.
_OCC_TAIL_LENGTH = 15  # 6 date + 1 type + 8 strike


def _utc_now() -> datetime:
    """Acquisition clock; module-level so tests can pin it deterministically."""
    return datetime.now(UTC)


def _sleep(seconds: float) -> None:
    """Rate-delay hook; module-level so tests can observe it."""
    _time.sleep(seconds)


def _optional_number(value: Any, field: str) -> float | None:
    """Return a finite float for a numeric value, None when missing.

    Raises ValueError for present values that are not finite numbers so
    malformed provider data fails explicitly instead of being coerced.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: non-numeric value {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field}: non-finite value {value!r}")
    return number


def _optional_quote(value: Any, field: str) -> float | None:
    """Like ``_optional_number`` but rejects negative prices."""
    number = _optional_number(value, field)
    if number is not None and number < 0:
        raise ValueError(f"{field}: negative value {value!r}")
    return number


def _optional_count(value: Any, field: str) -> int | None:
    """Return a non-negative integer count or None when missing."""
    number = _optional_number(value, field)
    if number is None:
        return None
    if number < 0:
        raise ValueError(f"{field}: negative value {value!r}")
    if not number.is_integer():
        raise ValueError(f"{field}: non-integral value {value!r}")
    return int(number)


def _normalize_symbol(value: str) -> str:
    """Normalize one configured request symbol for deterministic URLs."""
    return str(value).strip().upper()


def _expected_root(symbol: str) -> str:
    """OCC root for a request symbol (index chains are prefixed ``_``/``^``)."""
    return symbol.lstrip("_^")


def _parse_occ_symbol(
    contract_symbol: str, expected_root: str
) -> tuple[str, date, str, float] | None:
    """Decode an OCC contract symbol.

    Returns ``(root, expiration, option_type, strike)`` or None when the
    symbol is malformed (bad shape, invalid calendar date, zero strike,
    unknown side, root mismatch). ``None`` means the contract is rejected,
    never guessed at.
    """
    if not isinstance(contract_symbol, str) or len(contract_symbol) < (
        _OCC_TAIL_LENGTH + 1
    ):
        return None
    root = contract_symbol[:-_OCC_TAIL_LENGTH]
    tail = contract_symbol[-_OCC_TAIL_LENGTH:]
    try:
        expiration = date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
        strike = int(tail[7:15]) / 1000.0
    except ValueError:
        return None
    if root != expected_root or strike <= 0.0:
        return None
    if tail[6] == "C":
        option_type = "call"
    elif tail[6] == "P":
        option_type = "put"
    else:
        return None
    return root, expiration, option_type, strike


def _parse_source_timestamp(value: Any, timezone_name: str) -> datetime | None:
    """Parse the provider quote time into UTC.

    The raw value is Cboe exchange-local time; the configured source timezone
    (default America/Chicago) is attached before converting, and the raw
    string plus timezone are recorded in metadata so the assumption is
    explicit. An unparseable present timestamp fails the payload explicitly.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSourceData(f"source timestamp is not a string: {value!r}")
    raw = value.strip().replace("T", " ")
    parsed = None
    for fmt in _SOURCE_TIME_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise InvalidSourceData(f"unparseable source timestamp: {value!r}")
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)


def _optional_date(value: Any, field: str) -> date | None:
    """Parse an explicit provider-side expiration date, if the provider sends one."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field}: not a date {value!r}")
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field}: unparseable date {value!r}") from exc


class CboeOptionsCollector:
    source_id = SOURCE_ID

    def __init__(self):
        self.last_result_metadata: dict = {}

    # -- Collector contract -------------------------------------------------

    def collect(self, config: dict, correlation_id: str) -> list[dict]:
        cfg = config.get("collectors", {}).get(SOURCE_ID, {})
        symbols, symbols_truncated = self._configured_symbols(cfg)
        base_url = validate_configured_origin(
            cfg.get("base_url") or DEFAULT_BASE_URL,
            cfg,
            label="cboe_options base_url",
            canonical={DEFAULT_BASE_URL},
        )
        bounds = self._configured_bounds(cfg)
        source_timezone = self._configured_timezone(cfg)
        user_agent = str(cfg.get("user_agent") or DEFAULT_USER_AGENT)
        delay_minutes = self._configured_delay_minutes(cfg)

        captured_at = _utc_now()
        records: list[dict] = []
        feature_rows: list[dict] = []
        symbol_errors: list[dict] = []
        contracts_seen = 0
        contracts_kept = 0
        contracts_rejected = 0
        expiry_truncated = False
        contract_truncated = False
        first_error: Exception | None = None

        for index, symbol in enumerate(symbols):
            if index > 0:
                _sleep(bounds["rate_delay_seconds"])
            url = f"{base_url.rstrip('/')}/{symbol}.json"
            try:
                try:
                    response = make_request(
                        method="GET",
                        url=url,
                        headers={
                            "User-Agent": user_agent,
                            "Accept": "application/json",
                        },
                        correlation_id=correlation_id,
                        deadline_seconds=bounds["request_deadline_seconds"],
                        max_response_bytes=bounds["max_response_bytes"],
                    )
                except ResponseBodyTooLarge as exc:
                    raise InvalidSourceData(
                        f"cboe_options chain exceeds byte bound "
                        f"({bounds['max_response_bytes']} bytes)"
                    ) from exc
                self._enforce_byte_bound(response, bounds["max_response_bytes"])
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise InvalidSourceData(f"unparseable JSON from {url}") from exc
                chain_records, chain_stats = self._parse_chain_payload(
                    payload,
                    symbol=symbol,
                    url=url,
                    source_timezone=source_timezone,
                    captured_at=captured_at,
                    delay_minutes=delay_minutes,
                    max_contracts_per_symbol=bounds["max_contracts_per_symbol"],
                    max_expiries=bounds["max_expiries"],
                    symbols_truncated=symbols_truncated,
                )
                records.extend(chain_records)
                contracts_seen += chain_stats["contracts_seen"]
                contracts_kept += chain_stats["contracts_kept"]
                contracts_rejected += chain_stats["contracts_rejected"]
                expiry_truncated = expiry_truncated or chain_stats["expiries_truncated"]
                contract_truncated = (
                    contract_truncated or chain_stats["contracts_truncated"]
                )
                # One immutable feature row per analyzed snapshot: analytics
                # run over exactly the bounded validated contracts that are
                # persisted, never over rejected or truncated-away rows.
                feature_row = self._build_feature_row(
                    chain_records,
                    chain_stats,
                    symbol=symbol,
                    captured_at=captured_at,
                    symbols_truncated=symbols_truncated,
                )
                if feature_row is not None:
                    feature_rows.append(feature_row)
            except (httpx.HTTPError, InvalidSourceData) as exc:
                # A configured/dynamic research symbol may not have a Cboe
                # surface. Isolate provider rejection or malformed data to
                # that symbol; successful chains remain durable.
                transient = isinstance(exc, httpx.HTTPError)
                symbol_errors.append(
                    {
                        "symbol": symbol,
                        "stage": "chain_fetch" if transient else "chain_parse",
                        "code": "request_failed"
                        if transient
                        else "invalid_source_data",
                        "exception_type": type(exc).__name__,
                        "error_class": (
                            TransientSourceError.error_class
                            if transient
                            else InvalidSourceData.error_class
                        ),
                    }
                )
                if first_error is None:
                    first_error = exc
                logger.error(
                    "cboe_options_symbol_failed",
                    action="collect",
                    symbol=symbol,
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )

        if not records and symbol_errors and len(symbol_errors) == len(symbols):
            # Every requested symbol failed: the run failed explicitly.
            if isinstance(first_error, Exception):
                raise first_error
            raise InvalidSourceData("all cboe_options symbols failed")

        self.last_result_metadata = {
            "state": "partial_failure" if symbol_errors else "success",
            "captured_at": captured_at.isoformat(),
            "symbols_requested": len(symbols),
            "symbols_failed": len(symbol_errors),
            "symbols_truncated": symbols_truncated,
            "contracts_seen": contracts_seen,
            "contracts_kept": contracts_kept,
            "contracts_rejected": contracts_rejected,
            "expiries_truncated": expiry_truncated,
            "contracts_truncated": contract_truncated,
            "features_written": len(feature_rows),
            "symbol_errors": symbol_errors,
        }
        logger.info(
            "cboe_options_collected",
            action="collect",
            correlation_id=correlation_id,
            **{
                key: value
                for key, value in self.last_result_metadata.items()
                if key != "symbol_errors"
            },
        )
        additional_writes = []
        if feature_rows:
            additional_writes.append(
                CollectionWriteBatch(
                    table_name=FEATURE_TABLE,
                    records=feature_rows,
                    conflict_columns=["source", "symbol", "captured_at"],
                    insert_only=True,
                )
            )
        return CollectionResult(
            records=records,
            additional_writes=additional_writes,
            errors=symbol_errors,
            total_series=len(symbols),
            successful_series=len(symbols) - len(symbol_errors),
            metrics={"api_calls_made": len(symbols)},
        )

    def get_schedule(self, config: dict) -> str:
        return config["collectors"][SOURCE_ID]["schedule"]

    def health_check(self, config: dict) -> dict:
        started_at = _time.monotonic()

        def _latency() -> int:
            return max(0, round((_time.monotonic() - started_at) * 1000))

        try:
            cfg = config.get("collectors", {}).get(SOURCE_ID, {})
            symbols, _ = self._configured_symbols(cfg)
            base_url = validate_configured_origin(
                cfg.get("base_url") or DEFAULT_BASE_URL,
                cfg,
                label="cboe_options base_url",
                canonical={DEFAULT_BASE_URL},
            )
            user_agent = str(cfg.get("user_agent") or DEFAULT_USER_AGENT)
            url = f"{base_url.rstrip('/')}/{symbols[0]}.json"
            response = make_request(
                method="GET",
                url=url,
                headers={"User-Agent": user_agent, "Accept": "application/json"},
                max_retries=1,
                deadline_seconds=15.0,
            )
            if response.status_code == 200:
                return {
                    "healthy": True,
                    "message": "Cboe delayed quotes reachable",
                    "latency_ms": _latency(),
                }
            return {
                "healthy": False,
                "message": f"Cboe delayed quotes returned HTTP {response.status_code}",
                "latency_ms": _latency(),
            }
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            return {
                "healthy": False,
                "message": f"Cboe delayed quotes unreachable ({type(exc).__name__})",
                "latency_ms": _latency(),
            }

    def get_target_table(self) -> str:
        return "option_chain_snapshots"

    def get_conflict_columns(self) -> list[str]:
        return ["source", "contract_symbol", "captured_at"]

    # -- Configuration ------------------------------------------------------

    @staticmethod
    def _configured_symbols(cfg: dict) -> tuple[list[str], bool]:
        raw = cfg.get("symbols", [])
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValueError("cboe_options symbols are required (non-empty list)")
        max_symbols = CboeOptionsCollector._configured_bounds(cfg)["max_symbols"]
        symbols = [
            normalized
            for normalized in (_normalize_symbol(item) for item in raw)
            if normalized
        ]
        if not symbols:
            raise ValueError("cboe_options symbols are required (non-empty list)")
        truncated = len(symbols) > max_symbols
        return symbols[:max_symbols], truncated

    @staticmethod
    def _configured_bounds(cfg: dict) -> dict:
        bounds = dict(DEFAULT_BOUNDS)

        def _int_key(name: str, minimum: int, maximum: int | None = None) -> int:
            value = cfg.get(name, bounds[name])
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"cboe_options {name} must be an integer")
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError(
                    f"cboe_options {name} must be within [{minimum}, {maximum}]"
                )
            return value

        def _float_key(name: str, minimum: float) -> float:
            value = cfg.get(name, bounds[name])
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"cboe_options {name} must be a number")
            number = float(value)
            if not math.isfinite(number) or number < minimum:
                raise ValueError(f"cboe_options {name} must be >= {minimum}")
            return number

        bounds["max_symbols"] = _int_key("max_symbols", 1, 1000)
        bounds["max_contracts_per_symbol"] = _int_key("max_contracts_per_symbol", 1)
        bounds["max_expiries"] = _int_key("max_expiries", 1)
        bounds["max_response_bytes"] = _int_key("max_response_bytes", 1024)
        bounds["rate_delay_seconds"] = _float_key("rate_delay_seconds", 0.0)
        bounds["request_deadline_seconds"] = _float_key("request_deadline_seconds", 1.0)
        return bounds

    @staticmethod
    def _configured_timezone(cfg: dict) -> str:
        timezone_name = str(cfg.get("source_timezone") or DEFAULT_SOURCE_TIMEZONE)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"cboe_options source_timezone is unknown: {timezone_name!r}"
            ) from exc
        return timezone_name

    @staticmethod
    def _configured_delay_minutes(cfg: dict) -> int:
        value = cfg.get("delay_minutes", DEFAULT_DELAY_MINUTES)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                "cboe_options delay_minutes must be a non-negative integer"
            )
        return value

    # -- Fetching / parsing --------------------------------------------------

    @staticmethod
    def _enforce_byte_bound(response, max_bytes: int) -> None:
        """Reject an oversized declared body before consuming it.

        The actual byte count is enforced incrementally while the response
        streams (``max_response_bytes`` on the request); this early check
        only rejects an already-declared ``Content-Length`` so the body is
        never materialized at all.
        """
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise InvalidSourceData(
                        f"cboe_options chain exceeds byte bound "
                        f"(content-length {declared} > {max_bytes})"
                    )
            except (TypeError, ValueError):
                pass  # Unparseable header is not evidence of size; body check follows.

    @staticmethod
    def _parse_chain_payload(
        payload: Any,
        *,
        symbol: str,
        url: str,
        source_timezone: str,
        captured_at: datetime,
        delay_minutes: int,
        max_contracts_per_symbol: int,
        max_expiries: int,
        symbols_truncated: bool,
    ) -> tuple[list[dict], dict]:
        if not isinstance(payload, Mapping):
            raise InvalidSourceData("cboe_options payload is not a JSON object")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise InvalidSourceData("cboe_options payload is missing data object")
        options = data.get("options")
        if not isinstance(options, list):
            raise InvalidSourceData("cboe_options payload is missing options array")

        raw_source_time = data.get("timestamp")
        if raw_source_time is None:
            raw_source_time = payload.get("timestamp")
        source_timestamp = _parse_source_timestamp(raw_source_time, source_timezone)
        underlying_price = CboeOptionsCollector._chain_underlying_price(data)
        expected_root = _expected_root(symbol)

        kept: list[dict] = []
        rejected = 0
        seen: set[str] = set()
        rejection_reasons: list[tuple[str, str]] = []
        for item in options:
            if not isinstance(item, Mapping):
                rejected += 1
                if len(rejection_reasons) < 50:
                    rejection_reasons.append(("<non-object>", "not_an_object"))
                continue
            try:
                record, contract_key = CboeOptionsCollector._parse_contract(
                    item,
                    expected_root=expected_root,
                    symbol=symbol,
                    url=url,
                    source_timezone=source_timezone,
                    raw_source_time=raw_source_time,
                    source_timestamp=source_timestamp,
                    captured_at=captured_at,
                    delay_minutes=delay_minutes,
                    underlying_price=underlying_price,
                    data_symbol=data.get("symbol"),
                    security_type=data.get("security_type"),
                    symbols_truncated=symbols_truncated,
                )
            except ValueError as exc:
                rejected += 1
                if len(rejection_reasons) < 50:
                    contract_label = item.get("option", item.get("contract_symbol"))
                    rejection_reasons.append((str(contract_label), str(exc)))
                continue
            if contract_key in seen:
                rejected += 1
                if len(rejection_reasons) < 50:
                    rejection_reasons.append((contract_key, "duplicate_contract"))
                continue
            seen.add(contract_key)
            kept.append(record)

        if options and not kept:
            detail = "; ".join(
                f"{label}: {reason}" for label, reason in rejection_reasons[:20]
            )
            raise InvalidSourceData(
                f"cboe_options chain for {symbol} has no valid contracts ({detail})"
            )

        # Deterministic bounding: nearest-term expirations first, then a
        # bounded contract count, both in sorted (expiration, strike, side)
        # order so truncation is stable across identical payloads.
        kept.sort(
            key=lambda row: (row["expiration"], row["strike"], row["option_type"])
        )
        expirations: list[date] = []
        expirations_seen: set[date] = set()
        for row in kept:
            if row["expiration"] not in expirations_seen:
                expirations_seen.add(row["expiration"])
                expirations.append(row["expiration"])
        allowed_expirations = set(expirations[:max_expiries])
        expiries_truncated = len(expirations) > max_expiries
        bounded = [row for row in kept if row["expiration"] in allowed_expirations]
        contracts_truncated = len(bounded) > max_contracts_per_symbol
        bounded = bounded[:max_contracts_per_symbol]

        for row in bounded:
            row["metadata"]["truncated"]["expiries"] = expiries_truncated
            row["metadata"]["truncated"]["contracts"] = contracts_truncated

        stats = {
            "contracts_seen": len(options),
            "contracts_kept": len(bounded),
            "contracts_rejected": rejected,
            "expiries_truncated": expiries_truncated,
            "contracts_truncated": contracts_truncated,
        }
        return bounded, stats

    @staticmethod
    def _build_feature_row(
        chain_records: list[dict],
        chain_stats: dict,
        *,
        symbol: str,
        captured_at: datetime,
        symbols_truncated: bool,
    ) -> dict | None:
        """One immutable analytics feature row for a snapshot.

        Runs the pure analyzer over exactly the bounded validated contracts
        that will be persisted (rejected and truncated-away contracts are
        never analyzed) and preserves every explicit insufficient-data /
        insufficient-history state verbatim.  An empty chain is not a
        snapshot and yields no feature row.
        """
        if not chain_records:
            return None
        analytics = analyze_chain(chain_records)["symbols"][symbol]
        source_times = sorted(
            record["source_timestamp"]
            for record in chain_records
            if record.get("source_timestamp") is not None
        )
        first_metadata = chain_records[0].get("metadata")
        metadata = dict(first_metadata) if isinstance(first_metadata, Mapping) else {}
        metadata.update(
            {
                "contracts_seen": chain_stats["contracts_seen"],
                "contracts_kept": chain_stats["contracts_kept"],
                "contracts_rejected": chain_stats["contracts_rejected"],
                "analytics_source": "options_analytics.analyze_chain",
                "analyzed_contracts": len(chain_records),
                "symbols_truncated": symbols_truncated,
            }
        )
        return {
            "source": SOURCE_ID,
            "symbol": symbol,
            "captured_at": captured_at,
            "feature_version": FEATURE_VERSION,
            "source_timestamp_min": source_times[0] if source_times else None,
            "source_timestamp_max": source_times[-1] if source_times else None,
            "available_at": captured_at,
            "contract_count": len(chain_records),
            "analytics": analytics,
            "metadata": metadata,
        }

    @staticmethod
    def _chain_underlying_price(data: Mapping) -> float | None:
        """Underlying price: current_price, else quote.last_price, else
        underlying.last_price. A present but impossible value fails explicitly."""
        candidates: list[Any] = []
        if data.get("current_price") is not None:
            candidates.append(("current_price", data.get("current_price")))
        quote = data.get("quote")
        if isinstance(quote, Mapping) and quote.get("last_price") is not None:
            candidates.append(("quote.last_price", quote.get("last_price")))
        underlying = data.get("underlying")
        if isinstance(underlying, Mapping) and underlying.get("last_price") is not None:
            candidates.append(("underlying.last_price", underlying.get("last_price")))
        if not candidates:
            return None
        field, value = candidates[0]
        try:
            price = _optional_quote(value, f"underlying {field}")
        except ValueError as exc:
            raise InvalidSourceData(
                f"cboe_options underlying price invalid ({exc})"
            ) from exc
        return price

    @staticmethod
    def _parse_contract(
        item: Mapping,
        *,
        expected_root: str,
        symbol: str,
        url: str,
        source_timezone: str,
        raw_source_time: Any,
        source_timestamp: datetime | None,
        captured_at: datetime,
        delay_minutes: int,
        underlying_price: float | None,
        data_symbol: Any,
        security_type: Any,
        symbols_truncated: bool,
    ) -> tuple[dict, str]:
        option_field = item.get("option")
        symbol_field = item.get("contract_symbol")
        if option_field is not None and symbol_field is not None:
            if option_field != symbol_field:
                raise ValueError("conflicting option/contract_symbol fields")
            contract_symbol = option_field
        else:
            contract_symbol = option_field if option_field is not None else symbol_field
        decoded = _parse_occ_symbol(
            contract_symbol if isinstance(contract_symbol, str) else "", expected_root
        )
        if decoded is None:
            raise ValueError(f"malformed contract symbol {contract_symbol!r}")
        root, expiration, option_type, strike = decoded

        # If the provider ever starts sending explicit identity fields, they
        # must agree with the OCC decode; drift rejects the contract.
        explicit_expiration = _optional_date(item.get("expiration"), "expiration")
        if explicit_expiration is not None and explicit_expiration != expiration:
            raise ValueError(
                f"expiration field {explicit_expiration} conflicts with symbol decode"
            )
        explicit_strike = _optional_number(item.get("strike"), "strike")
        if explicit_strike is not None and explicit_strike != strike:
            raise ValueError(
                f"strike field {explicit_strike} conflicts with symbol decode"
            )

        bid = _optional_quote(item.get("bid"), "bid")
        ask = _optional_quote(item.get("ask"), "ask")
        if bid is not None and ask is not None and bid > ask:
            raise ValueError(f"crossed market bid {bid} > ask {ask}")
        last_value = item.get("last_trade_price")
        if last_value is None:
            last_value = item.get("last")
        last = _optional_quote(last_value, "last_trade_price")
        volume = _optional_count(item.get("volume"), "volume")
        open_interest = _optional_count(item.get("open_interest"), "open_interest")
        implied_volatility = _optional_quote(item.get("iv"), "iv")

        metadata = {
            "source": SOURCE_ID,
            "delayed": True,
            "delay_minutes": delay_minutes,
            "url": url,
            "request_symbol": symbol,
            "data_symbol": data_symbol if isinstance(data_symbol, str) else None,
            "security_type": (
                security_type if isinstance(security_type, str) else None
            ),
            "source_time_raw": raw_source_time
            if isinstance(raw_source_time, str)
            else None,
            "source_timezone": source_timezone,
            "acquisition_time": captured_at.isoformat(),
            "contract_root": root,
            "truncated": {
                "symbols": symbols_truncated,
                "expiries": False,
                "contracts": False,
            },
        }
        record = {
            "source": SOURCE_ID,
            "symbol": symbol,
            "contract_symbol": contract_symbol,
            "captured_at": captured_at,
            "source_timestamp": source_timestamp,
            "expiration": expiration,
            "strike": strike,
            "option_type": option_type,
            "bid": bid,
            "ask": ask,
            "last": last,
            "volume": volume,
            "open_interest": open_interest,
            "implied_volatility": implied_volatility,
            "underlying_price": underlying_price,
            "metadata": metadata,
        }
        return record, contract_symbol
