"""Keyless Nasdaq expectations, catalysts, ownership, and short positioning.

The collector snapshots only values returned by Nasdaq's public API. One
immutable daily document per symbol preserves point-in-time consensus,
institutional-holder flow, and reported short-interest history. Missing values
remain absent. Nasdaq does not publish borrow cost, utilization, or lendable
inventory through these endpoints, so the snapshot marks borrow unavailable
instead of treating short volume or short interest as borrow data.
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

from collectors.base import CollectionResult, elapsed_ms
from errors import InvalidSourceData, TransientSourceError, classify_error
from http_client import ResponseBodyTooLarge, make_request
from logging_config import get_logger
from provider_origins import validate_configured_origin

logger = get_logger("collector.company_expectations")

SOURCE_ID = "company_expectations"
DEFAULT_BASE_URL = "https://api.nasdaq.com/api"
DEFAULT_SCHEDULE = "15 12 * * 1-5"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CALENDAR_DAYS = 21
HARD_MAX_SYMBOLS = 200
MAX_RESPONSE_BYTES = 2_000_000
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
_FORECAST_FIELDS = (
    "fiscalEnd",
    "consensusEPSForecast",
    "highEPSForecast",
    "lowEPSForecast",
    "noOfEstimates",
    "up",
    "down",
)


def _section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    collectors = config.get("collectors")
    value = collectors.get(SOURCE_ID) if isinstance(collectors, Mapping) else None
    if not isinstance(value, Mapping):
        raise ValueError("company_expectations collector configuration is missing")
    return value


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
    text_value = str(value).strip().replace("$", "").replace(",", "")
    if not text_value or text_value.casefold() in {"n/a", "na", "--"}:
        return None
    negative = text_value.startswith("(") and text_value.endswith(")")
    if negative:
        text_value = text_value[1:-1]
    try:
        number = float(text_value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    if negative:
        number = -number
    return int(number) if number.is_integer() else number


def _forecast_rows(value: Any, *, maximum: int = 12) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in value[:maximum]:
        if not isinstance(raw, Mapping):
            continue
        fiscal_end = str(raw.get("fiscalEnd") or "").strip()
        if not fiscal_end:
            continue
        row: dict[str, Any] = {"fiscalEnd": fiscal_end[:40]}
        for field in _FORECAST_FIELDS[1:]:
            parsed = _number(raw.get(field))
            if parsed is not None:
                row[field] = parsed
        rows.append(row)
    return rows


def _calendar_row(raw: Mapping[str, Any], target_date: date) -> dict[str, Any] | None:
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(symbol):
        return None
    row: dict[str, Any] = {
        "symbol": symbol,
        "reportDate": target_date.isoformat(),
        "time": str(raw.get("time") or "").strip()[:80] or None,
        "name": str(raw.get("name") or "").strip()[:300] or None,
        "fiscalQuarterEnding": str(raw.get("fiscalQuarterEnding") or "").strip()[:40]
        or None,
    }
    for source, target in (
        ("epsForecast", "consensusEPSForecast"),
        ("noOfEsts", "noOfEstimates"),
        ("lastYearEPS", "lastYearEPS"),
    ):
        parsed = _number(raw.get(source))
        if parsed is not None:
            row[target] = parsed
    return row


def _institutional_positioning(payload: Any) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    summary = data.get("ownershipSummary")
    active = data.get("activePositions")
    new_sold = data.get("newSoldOutPositions")
    transactions = data.get("holdingsTransactions")
    if not isinstance(summary, Mapping):
        summary = {}

    def summary_value(key: str) -> Any:
        field = summary.get(key)
        return field.get("value") if isinstance(field, Mapping) else None

    def position_rows(section: Any) -> dict[str, dict[str, int | float]]:
        rows = section.get("rows") if isinstance(section, Mapping) else None
        output: dict[str, dict[str, int | float]] = {}
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            label = str(raw.get("positions") or "").strip().casefold().replace(" ", "_")
            if not label:
                continue
            values = {
                key: parsed
                for key in ("holders", "shares")
                if (parsed := _number(raw.get(key))) is not None
            }
            if values:
                output[label] = values
        return output

    ownership_text = str(summary_value("SharesOutstandingPCT") or "").strip()
    ownership_pct = _number(ownership_text.rstrip("%"))
    output: dict[str, Any] = {
        "institutional_ownership_pct": ownership_pct,
        "shares_outstanding_millions": _number(summary_value("ShareoutstandingTotal")),
        "holdings_value_millions_usd": _number(summary_value("TotalHoldingsValue")),
        "active_positions": position_rows(active),
        "new_and_sold_out_positions": position_rows(new_sold),
    }
    table = transactions.get("table") if isinstance(transactions, Mapping) else None
    rows = table.get("rows") if isinstance(table, Mapping) else None
    top_holders: list[dict[str, Any]] = []
    for raw in (rows if isinstance(rows, list) else [])[:10]:
        if not isinstance(raw, Mapping):
            continue
        holder = str(raw.get("ownerName") or "").strip()[:200]
        if not holder:
            continue
        row: dict[str, Any] = {
            "holder": holder,
            "report_date": str(raw.get("date") or "").strip()[:20] or None,
        }
        for source, target in (
            ("sharesHeld", "shares_held"),
            ("sharesChange", "shares_change"),
            ("sharesChangePCT", "shares_change_pct"),
            ("marketValue", "market_value_thousands_usd"),
        ):
            value = str(raw.get(source) or "").strip()
            parsed = _number(value.rstrip("%"))
            if parsed is not None:
                row[target] = parsed
        top_holders.append(row)
    output["top_holders"] = top_holders
    if not any(value not in (None, {}, []) for value in output.values()):
        return None
    return output


def _short_interest_history(payload: Any, *, maximum: int = 24) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    table = data.get("shortInterestTable") if isinstance(data, Mapping) else None
    raw_rows = table.get("rows") if isinstance(table, Mapping) else None
    rows: list[dict[str, Any]] = []
    for raw in (raw_rows if isinstance(raw_rows, list) else [])[:maximum]:
        if not isinstance(raw, Mapping):
            continue
        settlement = str(raw.get("settlementDate") or "").strip()
        try:
            report_date = datetime.strptime(settlement, "%m/%d/%Y").date()
        except ValueError:
            continue
        row: dict[str, Any] = {"settlement_date": report_date.isoformat()}
        for source, target in (
            ("interest", "short_interest_shares"),
            ("avgDailyShareVolume", "average_daily_share_volume"),
            ("daysToCover", "days_to_cover"),
        ):
            parsed = _number(raw.get(source))
            if parsed is not None:
                row[target] = parsed
        rows.append(row)
    return rows


def _document_id(symbol: str, as_of: date) -> str:
    return hashlib.sha256(
        f"nasdaq-expectations|{symbol}|{as_of.isoformat()}".encode()
    ).hexdigest()


def _content(
    symbol: str,
    quarterly: list[dict],
    yearly: list[dict],
    event: dict | None,
    institutional: dict[str, Any] | None,
    short_interest: list[dict[str, Any]],
) -> str:
    lines = [f"{symbol} consensus and positioning snapshot"]
    for horizon, rows in (("quarter", quarterly), ("year", yearly)):
        for row in rows:
            facts = [f"{key}={row[key]}" for key in _FORECAST_FIELDS if key in row]
            lines.append(f"{horizon}: " + "; ".join(facts))
    if event is not None:
        lines.append(
            "earnings catalyst: "
            + "; ".join(
                f"{key}={value}" for key, value in event.items() if value is not None
            )
        )
    if institutional is not None:
        facts = [
            f"{key}={value}"
            for key, value in institutional.items()
            if key != "top_holders" and value not in (None, {}, [])
        ]
        lines.append("institutional positioning: " + "; ".join(facts))
    if short_interest:
        lines.append(
            "reported short interest: "
            + "; ".join(f"{key}={value}" for key, value in short_interest[0].items())
        )
    lines.append(
        "borrow availability: unavailable from this public source; "
        "no borrow cost, utilization, or lendable inventory is inferred"
    )
    return "\n".join(lines)[:100_000]


class CompanyExpectationsCollector:
    source_id = SOURCE_ID

    def collect(
        self,
        config: Mapping[str, Any],
        correlation_id: str,
        *,
        now: datetime | None = None,
    ) -> CollectionResult:
        started = time.monotonic()
        section = _section(config)
        base_url = validate_configured_origin(
            section.get("base_url") or DEFAULT_BASE_URL,
            dict(section),
            label="company_expectations base_url",
            canonical={DEFAULT_BASE_URL},
        ).rstrip("/")
        raw_symbols = section.get("symbols")
        if not isinstance(raw_symbols, list):
            raise ValueError("company_expectations symbols must be an array")
        maximum = min(int(section.get("max_symbols") or 50), HARD_MAX_SYMBOLS)
        symbols: list[str] = []
        for raw in raw_symbols:
            symbol = str(raw).strip().upper()
            if _SYMBOL_RE.fullmatch(symbol) and symbol not in symbols:
                symbols.append(symbol)
        symbols = symbols[:maximum]
        if not symbols:
            raise ValueError("company_expectations requires at least one valid symbol")

        acquired_at = (now or datetime.now(UTC)).astimezone(UTC)
        timeout = float(section.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        concurrency = max(1, min(int(section.get("max_concurrency") or 8), 16))
        user_agent = str(section.get("user_agent") or DEFAULT_USER_AGENT)
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json,text/plain,*/*",
        }
        calendar_days = max(
            1,
            min(int(section.get("lookback_days") or DEFAULT_CALENDAR_DAYS), 60),
        )
        calendar: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        targets = [
            acquired_at.date() + timedelta(days=offset)
            for offset in range(calendar_days)
        ]
        api_calls = len(targets) + (len(symbols) * 3)

        def fetch_calendar(
            target: date,
        ) -> tuple[date, Any | None, Exception | None]:
            try:
                return (
                    target,
                    self._get_json(
                        f"{base_url}/calendar/earnings",
                        params={"date": target.isoformat()},
                        headers=headers,
                        timeout=timeout,
                        correlation_id=correlation_id,
                    ),
                    None,
                )
            except Exception as exc:
                return target, None, exc

        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(targets)),
            thread_name_prefix="company-expectations-calendar",
        ) as executor:
            for target, payload, error in executor.map(fetch_calendar, targets):
                if error is not None:
                    errors.append(self._error("calendar", target.isoformat(), error))
                    continue
                data = payload.get("data") if isinstance(payload, Mapping) else None
                rows = data.get("rows") if isinstance(data, Mapping) else None
                if not isinstance(rows, list):
                    errors.append(
                        self._error(
                            "calendar",
                            target.isoformat(),
                            InvalidSourceData(
                                "Nasdaq calendar success envelope is malformed"
                            ),
                        )
                    )
                    continue
                for raw in rows:
                    if not isinstance(raw, Mapping):
                        continue
                    parsed = _calendar_row(raw, target)
                    if parsed is not None and parsed["symbol"] in symbols:
                        calendar.setdefault(parsed["symbol"], parsed)

        records: list[dict[str, Any]] = []
        successful = 0

        def fetch_forecast(
            symbol: str,
        ) -> tuple[str, Any | None, Exception | None]:
            try:
                return (
                    symbol,
                    self._get_json(
                        f"{base_url}/analyst/{symbol}/earnings-forecast",
                        params=None,
                        headers=headers,
                        timeout=timeout,
                        correlation_id=correlation_id,
                    ),
                    None,
                )
            except Exception as exc:
                return symbol, None, exc

        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(symbols)),
            thread_name_prefix="company-expectations-forecast",
        ) as executor:
            forecast_results = executor.map(fetch_forecast, symbols)
            for symbol, payload, error in forecast_results:
                if error is not None:
                    errors.append(self._error("forecast", symbol, error))
                    continue
                try:
                    data = payload.get("data") if isinstance(payload, Mapping) else None
                    if not isinstance(data, Mapping):
                        raise InvalidSourceData(
                            "Nasdaq forecast success envelope is malformed"
                        )
                    quarterly_value = data.get("quarterlyForecast")
                    yearly_value = data.get("yearlyForecast")
                    quarterly = _forecast_rows(
                        quarterly_value.get("rows")
                        if isinstance(quarterly_value, Mapping)
                        else None
                    )
                    yearly = _forecast_rows(
                        yearly_value.get("rows")
                        if isinstance(yearly_value, Mapping)
                        else None
                    )
                    institutional: dict[str, Any] | None = None
                    short_interest: list[dict[str, Any]] = []
                    try:
                        institutional = _institutional_positioning(
                            self._get_json(
                                f"{base_url}/company/{symbol}/institutional-holdings",
                                params={"limit": 10},
                                headers=headers,
                                timeout=timeout,
                                correlation_id=correlation_id,
                            )
                        )
                    except Exception as exc:
                        errors.append(self._error("institutional", symbol, exc))
                    try:
                        short_interest = _short_interest_history(
                            self._get_json(
                                f"{base_url}/quote/{symbol}/short-interest",
                                params={"assetclass": "stocks", "limit": 24},
                                headers=headers,
                                timeout=timeout,
                                correlation_id=correlation_id,
                            )
                        )
                    except Exception as exc:
                        errors.append(self._error("short_interest", symbol, exc))
                    event = calendar.get(symbol)
                    successful += 1
                    if (
                        not quarterly
                        and not yearly
                        and event is None
                        and institutional is None
                        and not short_interest
                    ):
                        continue
                    institution = str((event or {}).get("name") or symbol).strip()[:300]
                    content = _content(
                        symbol,
                        quarterly,
                        yearly,
                        event,
                        institutional,
                        short_interest,
                    )
                    records.append(
                        {
                            "document_id": _document_id(symbol, acquired_at.date()),
                            "source": SOURCE_ID,
                            "institution": institution,
                            "document_type": "consensus_snapshot",
                            "title": f"{symbol} consensus, catalysts, and positioning",
                            "published_at": acquired_at,
                            "acquired_at": acquired_at,
                            "url": f"{base_url}/analyst/{symbol}/earnings-forecast",
                            "content": content,
                            "metadata": {
                                "ticker": symbol,
                                "as_of": acquired_at.isoformat(),
                                "quarterly": quarterly,
                                "yearly": yearly,
                                "next_earnings": event,
                                "institutional_positioning": institutional,
                                "short_interest": short_interest,
                                "borrow": {
                                    "available": False,
                                    "reason": (
                                        "public source does not publish borrow cost, "
                                        "utilization, or lendable inventory"
                                    ),
                                },
                                "provider": "nasdaq",
                                "point_in_time": True,
                            },
                        }
                    )
                except Exception as exc:
                    errors.append(self._error("forecast", symbol, exc))

        logger.info(
            "company_expectations_collection_completed",
            action="collect",
            symbols=len(symbols),
            records=len(records),
            errors=len(errors),
            api_calls=api_calls,
            duration_ms=elapsed_ms(started),
            correlation_id=correlation_id,
        )
        return CollectionResult(
            records=records,
            errors=errors,
            total_series=len(symbols),
            successful_series=successful,
            metrics={
                "api_calls_made": api_calls,
                "records": len(records),
                "calendar_matches": len(calendar),
            },
        )

    @staticmethod
    def _get_json(
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
        timeout: float,
        correlation_id: str,
    ) -> Any:
        response = make_request(
            method="GET",
            url=url,
            params=params,
            headers=dict(headers),
            timeout=timeout,
            max_retries=1,
            correlation_id=correlation_id,
            follow_redirects=True,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise InvalidSourceData("Nasdaq response is not JSON") from exc
        if not isinstance(payload, Mapping):
            raise InvalidSourceData("Nasdaq response JSON must be an object")
        return payload

    @staticmethod
    def _error(stage: str, item: str, exc: BaseException) -> dict[str, str]:
        if isinstance(exc, ResponseBodyTooLarge):
            error_class = InvalidSourceData.error_class
        else:
            policy = classify_error(exc)
            error_class = (
                TransientSourceError.error_class
                if isinstance(exc, httpx.HTTPError)
                else policy.error_class
            )
        return {
            "stage": stage,
            "item": item[:80],
            "code": "request_failed"
            if isinstance(exc, httpx.HTTPError)
            and not isinstance(exc, ResponseBodyTooLarge)
            else "invalid_data",
            "exception_type": type(exc).__name__,
            "error_class": error_class,
        }

    def get_schedule(self, config: Mapping[str, Any]) -> str:
        return str(_section(config).get("schedule") or DEFAULT_SCHEDULE)

    def health_check(self, config: Mapping[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        try:
            section = _section(config)
            base_url = validate_configured_origin(
                section.get("base_url") or DEFAULT_BASE_URL,
                dict(section),
                label="company_expectations base_url",
                canonical={DEFAULT_BASE_URL},
            ).rstrip("/")
            symbol = str((section.get("symbols") or ["AAPL"])[0]).strip().upper()
            self._get_json(
                f"{base_url}/analyst/{symbol}/earnings-forecast",
                params=None,
                headers={
                    "User-Agent": str(section.get("user_agent") or DEFAULT_USER_AGENT)
                },
                timeout=float(
                    section.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
                ),
                correlation_id="health-check",
            )
            return {"healthy": True, "message": "ok", "latency_ms": elapsed_ms(started)}
        except Exception:
            return {
                "healthy": False,
                "message": "unavailable",
                "latency_ms": elapsed_ms(started),
            }

    @staticmethod
    def get_target_table() -> str:
        return "source_documents"

    @staticmethod
    def get_conflict_columns() -> list[str]:
        return ["document_id"]


__all__ = ["CompanyExpectationsCollector", "SOURCE_ID"]
