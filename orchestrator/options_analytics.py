"""Pure option-chain analytics over captured snapshots.

Computes per-expiry ATM IV, implied move, put/call skew, volume and open
interest totals, and the ATM-IV term structure using ONLY captured snapshots
(records in the ``option_chain_snapshots`` shape). There is no I/O and no
clock: every value derives deterministically from the supplied rows.

Invariants:

- Missing values are never backfilled, imputed, or extrapolated. A metric
  that cannot be computed from the supplied data carries an explicit
  ``state``/``reason`` instead of a fabricated number.
- Historical claims (for example "unusual volume") require local history:
  either earlier captured-at groups inside the supplied snapshot rows or an
  explicit ``history`` parameter. Without enough history every historical
  metric reports ``insufficient_history`` with the available count.
- No dealer-gamma inference is performed; nothing in the output resembles a
  gamma exposure or dealer-position claim.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

STATE_OK = "ok"
STATE_NO_DATA = "no_data"
STATE_INSUFFICIENT_DATA = "insufficient_data"
STATE_INSUFFICIENT_HISTORY = "insufficient_history"

_VALID_OPTION_TYPES = frozenset({"call", "put"})


def _as_number(value: Any, field: str) -> float:
    """Strict numeric coercion; malformed rows fail explicitly."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}: non-numeric value {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field}: non-finite value {value!r}")
    return number


def _optional_number(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    return _as_number(value, field)


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"{field}: unparseable date {value!r}") from exc
    raise ValueError(f"{field}: not a date {value!r}")


def _as_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field}: unparseable datetime {value!r}") from exc
    raise ValueError(f"{field}: not a datetime {value!r}")


def _row_expiration(row: Mapping[str, Any]) -> date:
    return _as_date(row.get("expiration"), "expiration")


def _row_strike(row: Mapping[str, Any]) -> float:
    strike = _optional_number(row.get("strike"), "strike")
    if strike is None or strike <= 0.0:
        raise ValueError(f"strike: invalid value {row.get('strike')!r}")
    return strike


def _row_option_type(row: Mapping[str, Any]) -> str:
    option_type = row.get("option_type")
    if option_type not in _VALID_OPTION_TYPES:
        raise ValueError(f"option_type: invalid value {option_type!r}")
    return str(option_type)


def _mid(bid: float | None, ask: float | None, last: float | None) -> float | None:
    """Mid from a valid bid/ask pair, else the last trade, else None."""
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return last


def _group_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[datetime, list[Mapping[str, Any]]]]:
    """Group snapshot rows by symbol and captured_at (acquisition time)."""
    grouped: dict[str, dict[datetime, list[Mapping[str, Any]]]] = {}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"symbol: invalid value {symbol!r}")
        captured_at = _as_datetime(row.get("captured_at"), "captured_at")
        grouped.setdefault(symbol, {}).setdefault(captured_at, []).append(row)
    return grouped


def _contracts(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize rows into typed contract dicts, validating the shape."""
    contracts: list[dict[str, Any]] = []
    for row in rows:
        expiration = _row_expiration(row)
        strike = _row_strike(row)
        option_type = _row_option_type(row)
        bid = _optional_number(row.get("bid"), "bid")
        ask = _optional_number(row.get("ask"), "ask")
        last = _optional_number(row.get("last"), "last")
        volume = _optional_number(row.get("volume"), "volume")
        if volume is not None:
            if not volume.is_integer():
                raise ValueError(f"volume: non-integral value {volume!r}")
            volume = float(int(volume))
        open_interest = _optional_number(row.get("open_interest"), "open_interest")
        if open_interest is not None:
            if not open_interest.is_integer():
                raise ValueError(f"open_interest: non-integral value {open_interest!r}")
            open_interest = float(int(open_interest))
        implied_volatility = _optional_number(
            row.get("implied_volatility"), "implied_volatility"
        )
        contracts.append(
            {
                "expiration": expiration,
                "strike": strike,
                "option_type": option_type,
                "bid": bid,
                "ask": ask,
                "last": last,
                "volume": volume,
                "open_interest": open_interest,
                "implied_volatility": implied_volatility,
            }
        )
    contracts.sort(key=lambda c: (c["expiration"], c["strike"], c["option_type"]))
    return contracts


def _expiry_totals(
    contracts: Sequence[Mapping[str, Any]],
) -> tuple[float | None, bool, float | None, bool]:
    """Volume/OI totals over present values; completeness flags are explicit
    so a partial total is never mistaken for a complete one."""
    volumes = [c["volume"] for c in contracts if c["volume"] is not None]
    ois = [c["open_interest"] for c in contracts if c["open_interest"] is not None]
    volume = sum(volumes) if volumes else None
    open_interest = sum(ois) if ois else None
    return (
        volume,
        len(volumes) == len(contracts),
        open_interest,
        len(ois) == len(contracts),
    )


def _atm_selection(
    contracts: Sequence[Mapping[str, Any]], underlying_price: float
) -> dict[str, Any]:
    """Nearest strike (with reported IV) to the underlying price.

    Prefers the single strike closest to spot among contracts that carry an
    IV; when both sides at that strike carry IV the ATM IV is their mean, and
    ``sources`` records exactly which values were averaged. This is a
    selection over reported values — never an interpolation or imputation.
    """
    with_iv = [c for c in contracts if c["implied_volatility"] is not None]
    if not with_iv:
        return {
            "state": STATE_INSUFFICIENT_DATA,
            "reason": "atm_iv_missing",
            "strike": None,
            "iv": None,
            "sources": [],
        }
    atm = min(
        with_iv,
        key=lambda c: (
            abs(c["strike"] - underlying_price),
            c["strike"],
            c["option_type"],
        ),
    )
    at_strike = [
        c
        for c in with_iv
        if c["strike"] == atm["strike"] and c["option_type"] in {"call", "put"}
    ]
    by_side = {c["option_type"]: c["implied_volatility"] for c in at_strike}
    sources = sorted(by_side)
    values = [by_side[side] for side in sources]
    return {
        "state": STATE_OK,
        "reason": None,
        "strike": atm["strike"],
        "iv": sum(values) / len(values),
        "sources": sources,
    }


def _straddle(
    contracts: Sequence[Mapping[str, Any]], strike: float
) -> tuple[float | None, str | None]:
    """ATM straddle price from bid/ask mids (falling back to last) on both
    sides at one strike; None when either side lacks a price."""
    sides: dict[str, float | None] = {}
    for contract in contracts:
        if contract["strike"] == strike:
            side = contract["option_type"]
            mid = _mid(contract["bid"], contract["ask"], contract["last"])
            if side not in sides or (sides[side] is None and mid is not None):
                sides[side] = mid
    if "call" not in sides or "put" not in sides:
        return None, "straddle_unavailable"
    call_mid = sides["call"]
    put_mid = sides["put"]
    if call_mid is None or put_mid is None:
        return None, "straddle_unavailable"
    return call_mid + put_mid, None


def _put_call_skew(
    contracts: Sequence[Mapping[str, Any]],
    underlying_price: float,
    atm_iv: float | None,
    atm_strike: float | None,
    otm_offset: float,
) -> dict[str, Any]:
    """OTM put IV minus ATM IV in volatility points.

    The OTM put proxy is the strike nearest to ``underlying * (1 - otm_offset)``
    among puts strictly below the underlying price; the reported value is the
    difference between that contract's reported IV and the ATM IV. The proxy
    method is explicit so the number is never mistaken for a delta-fitted
    skew.
    """
    result: dict[str, Any] = {
        "value": None,
        "method": "otm_put_nearest_strike_proxy",
        "otm_offset": otm_offset,
        "otm_strike": None,
        "state": STATE_INSUFFICIENT_DATA,
        "reason": None,
    }
    if atm_iv is None or atm_strike is None:
        result["reason"] = "atm_iv_missing"
        return result
    target = underlying_price * (1.0 - otm_offset)
    otm_puts = [
        c
        for c in contracts
        if c["option_type"] == "put"
        and c["strike"] < underlying_price
        and c["implied_volatility"] is not None
    ]
    if not otm_puts:
        result["reason"] = "otm_put_missing"
        return result
    proxy = min(
        otm_puts,
        key=lambda c: (abs(c["strike"] - target), c["strike"]),
    )
    result["otm_strike"] = proxy["strike"]
    result["value"] = proxy["implied_volatility"] - atm_iv
    result["state"] = STATE_OK
    return result


def _analyze_expiry(
    contracts: Sequence[Mapping[str, Any]],
    *,
    underlying_price: float | None,
    as_of: date,
    otm_offset: float,
) -> dict[str, Any]:
    expiration = contracts[0]["expiration"]
    dte = (expiration - as_of).days
    volume, volume_complete, open_interest, oi_complete = _expiry_totals(contracts)
    n_calls = sum(1 for c in contracts if c["option_type"] == "call")
    n_puts = sum(1 for c in contracts if c["option_type"] == "put")

    entry: dict[str, Any] = {
        "expiration": expiration.isoformat(),
        "dte": dte,
        "expired": dte < 0,
        "state": STATE_OK,
        "reason": None,
        "n_contracts": len(contracts),
        "n_calls": n_calls,
        "n_puts": n_puts,
        "atm": None,
        "implied_move_pct": None,
        "implied_move_method": "atm_straddle_mid_relative_to_underlying",
        "implied_move_state": STATE_INSUFFICIENT_DATA,
        "implied_move_reason": None,
        "iv_move_pct": None,
        "iv_move_method": "atm_iv_annualized_to_dte",
        "straddle_price": None,
        "volume": volume,
        "open_interest": open_interest,
        "volume_complete": volume_complete,
        "oi_complete": oi_complete,
        "put_call_skew": {
            "value": None,
            "method": "otm_put_nearest_strike_proxy",
            "otm_offset": otm_offset,
            "otm_strike": None,
            "state": STATE_INSUFFICIENT_DATA,
            "reason": None,
        },
    }
    if dte < 0:
        entry["state"] = STATE_INSUFFICIENT_DATA
        entry["reason"] = "expired"
        return entry
    if underlying_price is None:
        entry["state"] = STATE_INSUFFICIENT_DATA
        entry["reason"] = "underlying_price_missing"
        entry["put_call_skew"]["reason"] = "underlying_price_missing"
        return entry

    atm = _atm_selection(contracts, underlying_price)
    entry["atm"] = atm
    if atm["state"] != STATE_OK:
        entry["state"] = STATE_INSUFFICIENT_DATA
        entry["reason"] = atm["reason"]
        entry["put_call_skew"]["reason"] = "atm_iv_missing"
        return entry

    straddle_price, straddle_reason = _straddle(contracts, atm["strike"])
    entry["straddle_price"] = straddle_price
    if straddle_price is not None:
        entry["implied_move_pct"] = straddle_price / underlying_price * 100.0
        entry["implied_move_state"] = STATE_OK
    else:
        entry["implied_move_reason"] = straddle_reason
    if dte > 0:
        entry["iv_move_pct"] = atm["iv"] * math.sqrt(dte / 365.0) * 100.0
    entry["put_call_skew"] = _put_call_skew(
        contracts,
        underlying_price,
        atm["iv"],
        atm["strike"],
        otm_offset,
    )
    return entry


def _analyze_unusualness(
    current_rows: Sequence[Mapping[str, Any]],
    history_groups: Sequence[Sequence[Mapping[str, Any]]],
    *,
    min_history_snapshots: int,
    unusual_threshold: float,
) -> dict[str, Any]:
    """Volume/OI unusualness versus LOCAL captured history only.

    Percentiles rank the current snapshot's totals against per-snapshot totals
    of earlier captured-at groups. Without ``min_history_snapshots`` prior
    snapshots the state is ``insufficient_history`` and no claim is made.
    """
    result: dict[str, Any] = {
        "state": STATE_INSUFFICIENT_HISTORY,
        "reason": (f"need_at_least_{min_history_snapshots}_prior_snapshots"),
        "available_history_snapshots": len(history_groups),
        "volume_percentile": None,
        "open_interest_percentile": None,
        "unusual_volume": None,
        "unusual_open_interest": None,
        "threshold": unusual_threshold,
        "scope": "symbol_totals_across_local_captured_snapshots",
        "local_history_only": True,
    }
    if len(history_groups) < min_history_snapshots:
        return result

    def _group_total(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
        values = [
            value
            for value in (_optional_number(row.get(field), field) for row in rows)
            if value is not None
        ]
        return sum(values) if values else None

    current_volume = _group_total(current_rows, "volume")
    current_oi = _group_total(current_rows, "open_interest")
    if current_volume is None or current_oi is None:
        result["reason"] = "current_snapshot_totals_incomplete"
        return result
    volume_history = [
        value
        for group in history_groups
        if (value := _group_total(group, "volume")) is not None
    ]
    oi_history = [
        value
        for group in history_groups
        if (value := _group_total(group, "open_interest")) is not None
    ]
    if (
        len(volume_history) < min_history_snapshots
        or len(oi_history) < min_history_snapshots
    ):
        result["reason"] = "history_snapshot_totals_incomplete"
        return result

    def _percentile(current: float, history: list[float]) -> float:
        at_or_below = sum(1 for value in history if value <= current)
        return at_or_below / len(history)

    volume_percentile = _percentile(current_volume, volume_history)
    oi_percentile = _percentile(current_oi, oi_history)
    result.update(
        {
            "state": STATE_OK,
            "reason": None,
            "volume_percentile": volume_percentile,
            "open_interest_percentile": oi_percentile,
            "unusual_volume": volume_percentile >= unusual_threshold,
            "unusual_open_interest": oi_percentile >= unusual_threshold,
        }
    )
    return result


def analyze_chain(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    history: Sequence[Mapping[str, Any]] | None = None,
    otm_offset: float = 0.05,
    min_history_snapshots: int = 5,
    unusual_threshold: float = 0.95,
) -> dict[str, Any]:
    """Analyze captured option-chain snapshot rows.

    ``snapshots`` is the current point-in-time data (one captured_at group per
    symbol; if several groups are supplied, the latest per symbol is treated
    as current and the earlier ones as local history). ``history`` optionally
    adds more local captured groups from prior runs. All inputs must already
    be validated collector records; malformed rows raise ``ValueError``.

    Returns a deterministic report with per-symbol expiries, ATM IV, implied
    move, put/call skew, volume/OI totals, the ATM-IV term structure, and a
    gated historical-unusualness section. Every uncomputable metric carries
    an explicit state and reason.
    """
    rows = list(snapshots)
    if not rows:
        return {"state": STATE_NO_DATA, "reason": "no_snapshots", "symbols": {}}
    if not 0.0 < otm_offset < 1.0:
        raise ValueError("otm_offset must be within (0, 1)")
    if min_history_snapshots < 1:
        raise ValueError("min_history_snapshots must be at least 1")
    if not 0.0 <= unusual_threshold <= 1.0:
        raise ValueError("unusual_threshold must be within [0, 1]")

    grouped = _group_rows(rows)
    if history:
        for symbol, time_groups in _group_rows(history).items():
            target = grouped.setdefault(symbol, {})
            for captured_at, history_rows in time_groups.items():
                existing = target.setdefault(captured_at, [])
                seen_keys = {
                    (row.get("symbol"), row.get("contract_symbol")) for row in existing
                }
                for row in history_rows:
                    key = (row.get("symbol"), row.get("contract_symbol"))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    existing.append(row)

    symbols: dict[str, Any] = {}
    for symbol, time_groups in grouped.items():
        captured_times = sorted(time_groups)
        current_time = captured_times[-1]
        current_rows = time_groups[current_time]
        history_groups = [
            time_groups[captured_at]
            for captured_at in captured_times[:-1]
            if captured_at != current_time
        ]

        contracts = _contracts(current_rows)
        underlying_price = None
        for row in current_rows:
            value = _optional_number(row.get("underlying_price"), "underlying_price")
            if value is None:
                continue
            if underlying_price is None:
                underlying_price = value
            elif value != underlying_price:
                raise ValueError(
                    f"inconsistent underlying_price within one snapshot: "
                    f"{value!r} vs {underlying_price!r}"
                )

        by_expiration: dict[date, list[dict[str, Any]]] = {}
        for contract in contracts:
            by_expiration.setdefault(contract["expiration"], []).append(contract)

        as_of = current_time.date()
        expiries = [
            _analyze_expiry(
                by_expiration[expiration],
                underlying_price=underlying_price,
                as_of=as_of,
                otm_offset=otm_offset,
            )
            for expiration in sorted(by_expiration)
        ]

        term_structure = [
            {
                "expiration": entry["expiration"],
                "dte": entry["dte"],
                "expired": entry["expired"],
                "atm_iv": entry["atm"]["iv"] if entry["atm"] else None,
                "state": (
                    STATE_OK
                    if entry["atm"] and entry["atm"]["state"] == STATE_OK
                    else STATE_INSUFFICIENT_DATA
                ),
                "reason": (
                    None
                    if entry["atm"] and entry["atm"]["state"] == STATE_OK
                    else entry["reason"]
                ),
            }
            for entry in expiries
        ]
        term_points = sum(1 for point in term_structure if point["state"] == STATE_OK)
        if term_points >= 2:
            term_structure_state = STATE_OK
            term_structure_reason = None
        else:
            term_structure_state = STATE_INSUFFICIENT_HISTORY
            term_structure_reason = "need_at_least_two_expiries_with_atm_iv"

        volume, volume_complete, open_interest, oi_complete = _expiry_totals(contracts)
        ok_expiries = [entry for entry in expiries if entry["state"] == STATE_OK]
        if ok_expiries:
            symbol_state = STATE_OK
            symbol_reason = None
        else:
            symbol_state = STATE_INSUFFICIENT_DATA
            symbol_reason = expiries[0]["reason"] if expiries else "no_contracts"

        source_timestamp = None
        for row in current_rows:
            value = row.get("source_timestamp")
            if value is not None:
                source_timestamp = _as_datetime(value, "source_timestamp")
                break

        symbols[symbol] = {
            "symbol": symbol,
            "captured_at": current_time.isoformat(),
            "source_timestamp": (
                source_timestamp.isoformat() if source_timestamp else None
            ),
            "underlying_price": underlying_price,
            "state": symbol_state,
            "reason": symbol_reason,
            "expiries": expiries,
            "term_structure": term_structure,
            "term_structure_state": term_structure_state,
            "term_structure_reason": term_structure_reason,
            "totals": {
                "volume": volume,
                "open_interest": open_interest,
                "volume_complete": volume_complete,
                "oi_complete": oi_complete,
                "n_contracts": len(contracts),
                "n_calls": sum(1 for c in contracts if c["option_type"] == "call"),
                "n_puts": sum(1 for c in contracts if c["option_type"] == "put"),
            },
            "unusualness": _analyze_unusualness(
                current_rows,
                history_groups,
                min_history_snapshots=min_history_snapshots,
                unusual_threshold=unusual_threshold,
            ),
        }

    overall = (
        STATE_OK
        if any(symbols[s]["state"] == STATE_OK for s in symbols)
        else STATE_INSUFFICIENT_DATA
    )
    return {
        "state": overall,
        "reason": None,
        "symbols": symbols,
    }
