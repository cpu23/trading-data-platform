from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable


HISTORY_LIMIT = 15


@dataclass(frozen=True)
class SignalSpec:
    series_id: str
    label: str
    value_unit: str
    change_unit: str
    higher_verb: str
    lower_verb: str
    flat_verb: str
    comparison_lag: int = 1
    tolerance: float = 0.0
    annual_periods: int | None = None
    level_state: Callable[[float, dict], str | None] | None = None


def _yield_curve_state(value: float, thresholds: dict) -> str:
    bands = thresholds.get("yield_curve", {})
    if value < bands.get("deep_inversion", -0.5):
        return "deeply inverted"
    if value < bands.get("inverted", 0.0):
        return "inverted"
    if value < bands.get("flat", 0.5):
        return "positive but flat"
    if value < bands.get("normal", 1.5):
        return "normally sloped"
    return "steep"


def _vix_state(value: float, thresholds: dict) -> str:
    bands = thresholds.get("vix", {})
    if value < bands.get("very_low", 12.0):
        return "very low volatility"
    if value < bands.get("low", 16.0):
        return "low volatility"
    if value < bands.get("moderate", 20.0):
        return "moderate volatility"
    if value < bands.get("elevated", 25.0):
        return "elevated volatility"
    if value < bands.get("high", 30.0):
        return "high volatility"
    return "very high volatility"


def _credit_state(value: float, thresholds: dict) -> str:
    bands = thresholds.get("credit_spread", {})
    if value < bands.get("tight", 3.0):
        return "tight credit"
    if value < bands.get("normal", 4.0):
        return "normal credit"
    if value < bands.get("widening", 5.0):
        return "wide credit"
    return "stressed credit"


def _breakeven_state(value: float, _thresholds: dict) -> str:
    if value < 1.5:
        return "very low inflation expectations"
    if value < 2.0:
        return "subdued inflation expectations"
    if value < 2.5:
        return "near-target inflation expectations"
    if value < 3.0:
        return "above-target inflation expectations"
    return "elevated inflation expectations"


SIGNAL_SPECS = (
    SignalSpec(
        "FEDFUNDS",
        "Federal funds rate",
        "percent",
        "percentage_points",
        "rose",
        "fell",
        "held",
        tolerance=0.005,
    ),
    SignalSpec(
        "GDPC1",
        "Real GDP annual growth",
        "percent_yoy",
        "percentage_points",
        "accelerated",
        "slowed",
        "was unchanged",
        tolerance=0.1,
        annual_periods=4,
    ),
    SignalSpec(
        "PAYEMS",
        "Nonfarm payroll employment",
        "thousand_jobs",
        "thousand_jobs",
        "gained",
        "lost",
        "was unchanged",
        tolerance=25.0,
    ),
    SignalSpec(
        "CPIAUCSL",
        "Headline CPI annual growth",
        "percent_yoy",
        "percentage_points",
        "accelerated",
        "slowed",
        "was unchanged",
        tolerance=0.02,
        annual_periods=12,
    ),
    SignalSpec(
        "PCEPILFE",
        "Core PCE annual growth",
        "percent_yoy",
        "percentage_points",
        "accelerated",
        "slowed",
        "was unchanged",
        tolerance=0.02,
        annual_periods=12,
    ),
    SignalSpec(
        "UNRATE",
        "Unemployment rate",
        "percent",
        "percentage_points",
        "rose",
        "fell",
        "held",
        tolerance=0.05,
    ),
    SignalSpec(
        "ICSA",
        "Initial jobless claims",
        "count",
        "count",
        "rose",
        "fell",
        "were unchanged",
        comparison_lag=4,
        tolerance=1.0,
    ),
    SignalSpec(
        "DGS2",
        "2-year Treasury yield",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
    ),
    SignalSpec(
        "DGS10",
        "10-year Treasury yield",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
    ),
    SignalSpec(
        "T10Y2Y",
        "10Y-2Y Treasury curve",
        "percentage_points",
        "basis_points",
        "steepened",
        "flattened",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
        level_state=_yield_curve_state,
    ),
    SignalSpec(
        "T10Y3M",
        "10Y-3M Treasury curve",
        "percentage_points",
        "basis_points",
        "steepened",
        "flattened",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
        level_state=_yield_curve_state,
    ),
    SignalSpec(
        "BAMLH0A0HYM2",
        "High-yield credit spread",
        "percentage_points",
        "basis_points",
        "widened",
        "tightened",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
        level_state=_credit_state,
    ),
    SignalSpec(
        "VIXCLS",
        "VIX",
        "index_points",
        "index_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.5,
        level_state=_vix_state,
    ),
    SignalSpec(
        "DTWEXBGS",
        "Broad trade-weighted US dollar",
        "index_points",
        "index_points",
        "strengthened",
        "weakened",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.2,
    ),
    SignalSpec(
        "T5YIE",
        "5-year breakeven inflation",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
        level_state=_breakeven_state,
    ),
    SignalSpec(
        "T10YIE",
        "10-year breakeven inflation",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
        level_state=_breakeven_state,
    ),
    SignalSpec(
        "M2SL",
        "M2 money stock annual growth",
        "percent_yoy",
        "percentage_points",
        "accelerated",
        "slowed",
        "was unchanged",
        tolerance=0.05,
        annual_periods=12,
    ),
    SignalSpec(
        "IRLTLT01GBM156N",
        "UK 10-year government bond yield",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        tolerance=0.02,
    ),
    SignalSpec(
        "OECD:CLI_US",
        "US composite leading indicator",
        "index_points",
        "index_points",
        "rose",
        "fell",
        "was unchanged",
        tolerance=0.05,
    ),
    SignalSpec(
        "OECD:CLI_DE",
        "German composite leading indicator",
        "index_points",
        "index_points",
        "rose",
        "fell",
        "was unchanged",
        tolerance=0.05,
    ),
    SignalSpec(
        "OECD:CLI_GB",
        "UK composite leading indicator",
        "index_points",
        "index_points",
        "rose",
        "fell",
        "was unchanged",
        tolerance=0.05,
    ),
    SignalSpec(
        "OECD:CLI_JP",
        "Japan composite leading indicator",
        "index_points",
        "index_points",
        "rose",
        "fell",
        "was unchanged",
        tolerance=0.05,
    ),
    SignalSpec(
        "ECB:DEPOSIT_RATE",
        "ECB deposit facility rate",
        "percent",
        "percentage_points",
        "rose",
        "fell",
        "held",
        tolerance=0.005,
    ),
    SignalSpec(
        "ECB:ESTR",
        "Euro short-term rate",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.02,
    ),
    SignalSpec(
        "ECB:CISS",
        "Euro-area systemic stress index",
        "index_points",
        "index_points",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=0.01,
    ),
    SignalSpec(
        "ECB:CREDIT_NFC",
        "Euro-area corporate credit annual growth",
        "percent_yoy",
        "percentage_points",
        "accelerated",
        "slowed",
        "was unchanged",
        tolerance=0.1,
        annual_periods=12,
    ),
    SignalSpec(
        "ECB:GOVT_10Y",
        "Euro-area 10-year government yield",
        "percent",
        "basis_points",
        "rose",
        "fell",
        "was unchanged",
        tolerance=0.02,
    ),
    SignalSpec(
        "BOE:BANK_RATE",
        "Bank of England policy rate",
        "percent",
        "percentage_points",
        "rose",
        "fell",
        "held",
        comparison_lag=5,
        tolerance=0.005,
    ),
    SignalSpec(
        "BOE:M4",
        "UK broad money M4 monthly flow",
        "million_pounds",
        "million_pounds",
        "increased",
        "decreased",
        "was unchanged",
        tolerance=5000.0,
    ),
    SignalSpec(
        "BOE:TOTAL_LENDING_INDIVIDUALS",
        "UK total lending to individuals annual growth",
        "percent_yoy",
        "percentage_points",
        "accelerated",
        "slowed",
        "was unchanged",
        tolerance=0.1,
        annual_periods=12,
    ),
    SignalSpec(
        "BOE:MORTGAGE_APPROVALS",
        "UK mortgage approvals for house purchase",
        "count",
        "count",
        "rose",
        "fell",
        "were unchanged",
        tolerance=1000.0,
    ),
    SignalSpec(
        "DCOILBRENTEU",
        "Brent crude oil spot price",
        "dollars",
        "dollars",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=1.0,
    ),
    SignalSpec(
        "DCOILWTICO",
        "WTI crude oil spot price",
        "dollars",
        "dollars",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=5,
        tolerance=1.0,
    ),
    SignalSpec(
        "EIA:CRUDE_STOCKS",
        "US crude oil inventories",
        "thousand_barrels",
        "thousand_barrels",
        "rose",
        "fell",
        "were unchanged",
        comparison_lag=4,
        tolerance=5000.0,
    ),
    SignalSpec(
        "EIA:NATGAS_STORAGE",
        "US working natural gas storage",
        "billion_cubic_feet",
        "billion_cubic_feet",
        "rose",
        "fell",
        "was unchanged",
        comparison_lag=4,
        tolerance=50.0,
    ),
)


def analyze_macro_trends(
    macro_data: dict, thresholds: dict | None = None
) -> list[dict]:
    """Return deterministic, evidence-linked states for configured macro series."""
    resolved_thresholds = thresholds or {}
    signals = []
    for spec in SIGNAL_SPECS:
        observations = _normalized_observations(macro_data.get(spec.series_id, {}))
        values = _derived_values(observations, spec)
        if len(values) < 2:
            continue
        signal = _build_signal(spec, values, resolved_thresholds)
        if signal is not None:
            signals.append(signal)
    return signals


def format_trend_signals(signals: list[dict]) -> str:
    if not signals:
        return "No deterministic trend signals had sufficient history."
    return "\n".join(f"- {signal['statement']}" for signal in signals)


def build_macro_synthesis(signals: list[dict]) -> dict:
    """Combine atomic observations into deterministic economic domain states."""
    by_id = {signal["series_id"]: signal for signal in signals}
    domains = {
        "policy": _single_domain(by_id, "FEDFUNDS", "tightening", "easing"),
        "us_growth": _consensus_domain(
            by_id,
            ("GDPC1", "PAYEMS", "OECD:CLI_US"),
            "strengthening",
            "weakening",
        ),
        "global_growth": _consensus_domain(
            by_id,
            ("OECD:CLI_DE", "OECD:CLI_GB", "OECD:CLI_JP"),
            "strengthening",
            "weakening",
        ),
        "inflation": _consensus_domain(
            by_id, ("CPIAUCSL", "PCEPILFE"), "heating", "cooling"
        ),
        "labor": _consensus_domain(
            by_id, ("UNRATE", "ICSA"), "weakening", "strengthening"
        ),
        "market_rates": _consensus_domain(
            by_id, ("DGS2", "DGS10"), "rising", "falling"
        ),
        "financial_conditions": _consensus_domain(
            by_id, ("BAMLH0A0HYM2", "VIXCLS"), "tightening", "easing"
        ),
        "dollar": _single_domain(by_id, "DTWEXBGS", "strengthening", "weakening"),
        "euro_policy": _single_domain(
            by_id, "ECB:DEPOSIT_RATE", "tightening", "easing"
        ),
        "euro_market_rates": _consensus_domain(
            by_id, ("ECB:ESTR", "ECB:GOVT_10Y"), "rising", "falling"
        ),
        "euro_financial_stress": _single_domain(
            by_id, "ECB:CISS", "tightening", "easing"
        ),
        "euro_credit": _single_domain(
            by_id, "ECB:CREDIT_NFC", "expanding", "contracting"
        ),
        "uk_growth": _single_domain(by_id, "OECD:CLI_GB", "strengthening", "weakening"),
        "uk_policy": _single_domain(by_id, "BOE:BANK_RATE", "tightening", "easing"),
        "uk_market_rates": _single_domain(
            by_id, "IRLTLT01GBM156N", "rising", "falling"
        ),
        "uk_money": _single_domain(by_id, "BOE:M4", "expanding", "contracting"),
        "uk_credit": _consensus_domain(
            by_id,
            ("BOE:TOTAL_LENDING_INDIVIDUALS", "BOE:MORTGAGE_APPROVALS"),
            "expanding",
            "contracting",
        ),
        "energy_prices": _consensus_domain(
            by_id,
            ("DCOILBRENTEU", "DCOILWTICO"),
            "rising",
            "falling",
        ),
        "energy_inventories": _consensus_domain(
            by_id,
            ("EIA:CRUDE_STOCKS", "EIA:NATGAS_STORAGE"),
            "building",
            "drawing",
        ),
    }
    core_domains = (
        "policy",
        "us_growth",
        "inflation",
        "labor",
        "market_rates",
        "financial_conditions",
    )
    available_core = sum(
        domains[name]["state"] != "insufficient_data" for name in core_domains
    )
    confidence_ceiling = "low" if available_core < 3 else "moderate"
    composite_state = _composite_state(domains, available_core)
    real_rate_proxy = _real_rate_proxy(by_id)
    transmission = _transmission_channels(domains)
    reversal_conditions = [
        condition
        for series_id in (
            "CPIAUCSL",
            "PCEPILFE",
            "UNRATE",
            "ICSA",
            "DGS10",
            "BAMLH0A0HYM2",
            "VIXCLS",
        )
        if (condition := _reversal_condition(by_id.get(series_id)))
    ][:4]

    return {
        "composite_state": composite_state,
        "confidence_ceiling": confidence_ceiling,
        "available_core_domains": available_core,
        "total_core_domains": len(core_domains),
        "domains": domains,
        "real_rate_proxy": real_rate_proxy,
        "transmission_channels": transmission,
        "reversal_conditions": reversal_conditions,
    }


def format_macro_synthesis(synthesis: dict) -> str:
    domains = synthesis.get("domains", {})
    domain_text = "; ".join(
        f"{name.replace('_', ' ')}={value.get('state', 'insufficient_data')}"
        for name, value in domains.items()
    )
    lines = [
        (
            f"Composite state: {synthesis.get('composite_state', 'insufficient_coverage')}; "
            f"confidence ceiling: {synthesis.get('confidence_ceiling', 'low')} "
            f"({synthesis.get('available_core_domains', 0)}/"
            f"{synthesis.get('total_core_domains', 5)} core domains available)."
        ),
        f"Domain states: {domain_text}.",
    ]
    real_rate_proxy = synthesis.get("real_rate_proxy")
    if isinstance(real_rate_proxy, dict) and real_rate_proxy.get("statement"):
        lines.append(real_rate_proxy["statement"])
    else:
        lines.append(
            "Maturity-matched real-rate proxy: unavailable; no real-rate direction "
            "can be established from the supplied series."
        )
    channels = synthesis.get("transmission_channels", [])
    if channels:
        lines.append("Economic transmission:")
        lines.extend(f"- {channel}" for channel in channels)
    reversal_conditions = synthesis.get("reversal_conditions", [])
    if reversal_conditions:
        lines.append("Observable reversal conditions:")
        lines.extend(f"- {condition}" for condition in reversal_conditions)
    return "\n".join(lines)


def _single_domain(
    by_id: dict[str, dict], series_id: str, higher_state: str, lower_state: str
) -> dict:
    signal = by_id.get(series_id)
    if signal is None:
        return {"state": "insufficient_data", "series_ids": []}
    state = {
        "higher": higher_state,
        "lower": lower_state,
        "unchanged": "stable",
    }[signal["direction"]]
    return {"state": state, "series_ids": [series_id]}


def _consensus_domain(
    by_id: dict[str, dict],
    series_ids: tuple[str, ...],
    higher_state: str,
    lower_state: str,
) -> dict:
    available = [by_id[series_id] for series_id in series_ids if series_id in by_id]
    if not available:
        return {"state": "insufficient_data", "series_ids": []}
    material = {signal["direction"] for signal in available} - {"unchanged"}
    if not material:
        state = "stable"
    elif material == {"higher"}:
        state = higher_state
    elif material == {"lower"}:
        state = lower_state
    else:
        state = "conflicting"
    return {
        "state": state,
        "series_ids": [signal["series_id"] for signal in available],
    }


def _composite_state(domains: dict, available_core: int) -> str:
    if available_core < 3:
        return "insufficient_coverage"
    policy = domains["policy"]["state"]
    growth = domains["us_growth"]["state"]
    inflation = domains["inflation"]["state"]
    labor = domains["labor"]["state"]
    financial = domains["financial_conditions"]["state"]
    if policy == "easing" and financial == "tightening":
        return "policy_easing_with_market_stress"
    if inflation == "heating" and financial == "tightening":
        return "inflationary_tightening"
    if inflation == "cooling" and (labor == "weakening" or growth == "weakening"):
        return "disinflationary_slowdown"
    if (
        inflation == "cooling"
        and growth in {"stable", "strengthening"}
        and labor in {"stable", "strengthening"}
        and financial in {"stable", "easing"}
    ):
        return "soft_landing_configuration"
    return "mixed_transition"


def _real_rate_proxy(by_id: dict[str, dict]) -> dict | None:
    nominal = by_id.get("DGS10")
    breakeven = by_id.get("T10YIE")
    if nominal is None or breakeven is None:
        return None
    latest = nominal["latest"]["value"] - breakeven["latest"]["value"]
    reference = nominal["reference"]["value"] - breakeven["reference"]["value"]
    change_bps = (latest - reference) * 100.0
    direction = _direction(change_bps, 2.0)
    verb = {"higher": "rose", "lower": "fell", "unchanged": "was unchanged"}[direction]
    return {
        "series_id": "DGS10_MINUS_T10YIE",
        "label": "Approximate 10-year real-rate proxy",
        "latest": round(latest, 4),
        "reference": round(reference, 4),
        "direction": direction,
        "change_basis_points": round(change_bps, 2),
        "statement": (
            "Approximate maturity-matched 10-year real-rate proxy "
            f"(DGS10 minus T10YIE) {verb} by {abs(change_bps):.0f} basis points "
            f"to {latest:.2f}% from {reference:.2f}% over the supplied comparable windows."
        ),
    }


def _transmission_channels(domains: dict) -> list[str]:
    channels = []
    rates = domains["market_rates"]["state"]
    financial = domains["financial_conditions"]["state"]
    labor = domains["labor"]["state"]
    inflation = domains["inflation"]["state"]
    growth = domains["us_growth"]["state"]
    energy = domains["energy_prices"]["state"]
    uk_policy = domains["uk_policy"]["state"]
    euro_policy = domains["euro_policy"]["state"]
    if growth == "strengthening":
        channels.append(
            "Growth channel: US activity indicators strengthened, supporting income "
            "and demand resilience."
        )
    elif growth == "weakening":
        channels.append(
            "Growth channel: US activity indicators weakened, increasing downside "
            "risk to income and demand."
        )
    if rates == "rising":
        channels.append(
            "Borrowing-cost channel: Treasury yields rose, increasing market-rate "
            "pressure on rate-sensitive households and firms."
        )
    elif rates == "falling":
        channels.append(
            "Borrowing-cost channel: Treasury yields fell, reducing market-rate "
            "pressure on rate-sensitive households and firms."
        )
    if financial == "tightening":
        channels.append(
            "Corporate-finance channel: credit spreads and/or volatility rose, "
            "tightening market financing conditions and risk tolerance."
        )
    elif financial == "easing":
        channels.append(
            "Corporate-finance channel: credit spreads and/or volatility fell, "
            "easing market financing conditions and risk tolerance."
        )
    if labor == "weakening":
        channels.append(
            "Household-income channel: labor indicators weakened, increasing downside "
            "risk to income growth and consumption."
        )
    elif labor == "strengthening":
        channels.append(
            "Household-income channel: labor indicators strengthened, supporting "
            "income growth and consumption resilience."
        )
    if inflation == "heating":
        channels.append(
            "Margin-and-demand channel: inflation accelerated, increasing input-cost "
            "and purchasing-power pressure unless nominal income keeps pace."
        )
    elif inflation == "cooling":
        channels.append(
            "Margin-and-demand channel: inflation cooled, reducing price pressure and "
            "supporting purchasing power if nominal income holds."
        )
    if energy == "rising":
        channels.append(
            "Energy-cost channel: Brent and/or WTI prices rose, increasing input-cost "
            "pressure for energy-intensive households and firms."
        )
    elif energy == "falling":
        channels.append(
            "Energy-cost channel: Brent and/or WTI prices fell, reducing direct "
            "energy-cost pressure."
        )
    if uk_policy == "tightening":
        channels.append(
            "UK policy channel: Bank Rate rose, increasing sterling borrowing-cost "
            "pressure."
        )
    elif uk_policy == "easing":
        channels.append(
            "UK policy channel: Bank Rate fell, reducing sterling borrowing-cost "
            "pressure."
        )
    if euro_policy == "tightening":
        channels.append(
            "Euro policy channel: the ECB deposit rate rose, increasing euro-area "
            "borrowing-cost pressure."
        )
    elif euro_policy == "easing":
        channels.append(
            "Euro policy channel: the ECB deposit rate fell, reducing euro-area "
            "borrowing-cost pressure."
        )
    return channels


def _reversal_condition(signal: dict | None) -> str | None:
    if signal is None or signal.get("direction") == "unchanged":
        return None
    reference = signal["reference"]["value"]
    value_unit = signal.get("value_unit", "index_points")
    threshold = _format_value(reference, value_unit)
    reverse_verb = "below" if signal["direction"] == "higher" else "above"
    return (
        f"A comparable-window {signal['label']} reading {reverse_verb} {threshold} "
        f"would reverse the current {signal['direction']} direction."
    )


def _normalized_observations(entry: dict) -> list[dict]:
    history = entry.get("history", []) if isinstance(entry, dict) else []
    usable = [row for row in history if row.get("value") is not None]
    usable.sort(key=lambda row: _date_text(row.get("observed_at")), reverse=True)
    return usable[:HISTORY_LIMIT]


def _derived_values(observations: list[dict], spec: SignalSpec) -> list[dict]:
    if spec.annual_periods is None:
        return [
            {
                "value": float(row["value"]),
                "observed_at": _date_text(row.get("observed_at")),
            }
            for row in observations
        ]

    periods = spec.annual_periods
    derived = []
    for index in range(len(observations) - periods):
        current = float(observations[index]["value"])
        prior = float(observations[index + periods]["value"])
        if prior == 0:
            continue
        derived.append(
            {
                "value": ((current / prior) - 1.0) * 100.0,
                "observed_at": _date_text(observations[index].get("observed_at")),
            }
        )
    return derived


def _build_signal(
    spec: SignalSpec, values: list[dict], thresholds: dict
) -> dict | None:
    lag = spec.comparison_lag if len(values) > spec.comparison_lag else 1
    if len(values) <= lag:
        return None

    latest = values[0]
    reference = values[lag]
    delta = latest["value"] - reference["value"]
    direction = _direction(delta, spec.tolerance)

    prior_direction = None
    if len(values) > lag * 2:
        prior_delta = reference["value"] - values[lag * 2]["value"]
        prior_direction = _direction(prior_delta, spec.tolerance)
    transition = _transition(direction, prior_direction)

    level_state = (
        spec.level_state(latest["value"], thresholds) if spec.level_state else None
    )
    change_value = delta * 100.0 if spec.change_unit == "basis_points" else delta
    statement = _statement(
        spec,
        latest=latest,
        reference=reference,
        direction=direction,
        transition=transition,
        change_value=change_value,
        level_state=level_state,
        lag=lag,
    )
    return {
        "series_id": spec.series_id,
        "label": spec.label,
        "value_unit": spec.value_unit,
        "latest": {
            "value": round(latest["value"], 4),
            "observed_at": latest["observed_at"],
        },
        "reference": {
            "value": round(reference["value"], 4),
            "observed_at": reference["observed_at"],
        },
        "comparison_observations": lag,
        "direction": direction,
        "transition": transition,
        "change": {"value": round(change_value, 2), "unit": spec.change_unit},
        "level_state": level_state,
        "statement": statement,
    }


def _direction(delta: float, tolerance: float) -> str:
    if delta > tolerance:
        return "higher"
    if delta < -tolerance:
        return "lower"
    return "unchanged"


def _transition(direction: str, prior_direction: str | None) -> str:
    if prior_direction is None:
        return "not_comparable"
    if direction == "unchanged":
        return "stable" if prior_direction == "unchanged" else "stalled"
    if prior_direction == direction:
        return "persisting"
    if prior_direction == "unchanged":
        return "emerging"
    return "reversal"


def _statement(
    spec: SignalSpec,
    *,
    latest: dict,
    reference: dict,
    direction: str,
    transition: str,
    change_value: float,
    level_state: str | None,
    lag: int,
) -> str:
    latest_text = _format_value(latest["value"], spec.value_unit)
    reference_text = _format_value(reference["value"], spec.value_unit)
    window = "the prior observation" if lag == 1 else f"{lag} observations"
    if direction == "unchanged":
        movement = (
            f"{spec.flat_verb} at {latest_text} versus {reference_text} over {window}"
        )
    else:
        verb = spec.higher_verb if direction == "higher" else spec.lower_verb
        movement = (
            f"{verb} {_format_change(abs(change_value), spec.change_unit)} over {window} "
            f"to {latest_text} from {reference_text}"
        )

    state_suffix = f"; level state: {level_state}" if level_state else ""
    transition_suffix = {
        "persisting": "; the move persisted from the preceding window",
        "reversal": "; this reversed the preceding window's direction",
        "emerging": "; the move emerged after a stable preceding window",
        "stalled": "; the preceding move stalled",
        "stable": "; the series was stable across both windows",
        "not_comparable": "; earlier-window direction was unavailable",
    }[transition]
    return (
        f"{spec.label} {movement} ({reference['observed_at']} to {latest['observed_at']})"
        f"{state_suffix}{transition_suffix}."
    )


def _format_value(value: float, unit: str) -> str:
    if unit in {"percent", "percent_yoy", "percentage_points"}:
        return f"{value:.2f}%"
    if unit == "count":
        return f"{value:,.0f}"
    if unit == "thousand_jobs":
        return f"{value:,.0f} thousand jobs"
    if unit == "thousand_barrels":
        return f"{value:,.0f} thousand barrels"
    if unit == "billion_cubic_feet":
        return f"{value:,.0f} billion cubic feet"
    if unit == "dollars":
        return f"${value:.2f}"
    if unit == "million_pounds":
        return f"£{value:,.0f} million"
    return f"{value:.2f}"


def _format_change(value: float, unit: str) -> str:
    if unit == "basis_points":
        return f"{value:.0f} basis points"
    if unit == "percentage_points":
        return f"{value:.2f} percentage points"
    if unit == "million_pounds":
        return f"£{value:,.0f} million"
    if unit == "count":
        return f"{value:,.0f}"
    if unit == "thousand_jobs":
        return f"{value:,.0f} thousand jobs"
    if unit == "thousand_barrels":
        return f"{value:,.0f} thousand barrels"
    if unit == "billion_cubic_feet":
        return f"{value:,.0f} billion cubic feet"
    if unit == "dollars":
        return f"${value:.2f}"
    return f"{value:.2f} index points"


def _date_text(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "unknown")
