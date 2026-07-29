"""Pure, deterministic investment-report analysis rules.

The module deliberately contains no I/O.  Model output is treated as untrusted
input: only finite numeric values are allowed into arithmetic, and all missing
inputs remain explicit ``None`` values in the result.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from typing import Any, Mapping


CURRENCY_CODES = ("USD", "EUR", "GBP", "JPY", "CNY", "KRW", "TWD", "HKD", "SGD", "INR")


STANDARD_METRICS = (
    "revenue",
    "operating_cash_flow",
    "capex",
    "net_income",
    "diluted_eps",
    "shares_outstanding",
    "market_price",
    "net_debt",
    "gross_margin",
    "inventory",
    "backlog",
)

# Weights are intentionally part of the public deterministic table.  A score
# is a weighted contribution (not a probability or an LLM confidence value).
SIGNAL_RULES = OrderedDict(
    (
        (
            "revenue",
            {
                "weight": 2,
                "rule": "change >= 10%: +2; change > 0%: +1; change < -10%: -2; otherwise -1",
            },
        ),
        (
            "capex",
            {
                "weight": 2,
                "rule": "capex growth >= 15%: +2; growth > 0%: +1; decline <= -10%: -2; otherwise -1",
            },
        ),
        (
            "backlog",
            {
                "weight": 1,
                "rule": "change >= 10%: +1; positive change: +1; negative change: -1",
            },
        ),
        (
            "inventory_vs_revenue",
            {
                "weight": 2,
                "rule": "inventory growth above revenue growth: -2 (above 5pp), otherwise -1; below: +1",
            },
        ),
        (
            "fcf",
            {
                "weight": 2,
                "rule": "positive FCF growth/margin: +2; negative FCF or deterioration: -2",
            },
        ),
        (
            "gross_margin_delta",
            {
                "weight": 2,
                "rule": "margin expansion >= 1pp: +2; contraction <= -1pp: -2; otherwise +/-1",
            },
        ),
        (
            "ai_demand",
            {"weight": 1, "rule": "reported AI demand present; strength maps weak/moderate/strong to +1"},
        ),
        (
            "data_centre_demand",
            {"weight": 1, "rule": "reported data-centre demand present; strength maps weak/moderate/strong to +1"},
        ),
        (
            "supply_constraints",
            {"weight": 1, "rule": "reported supply constraint supports cycle pricing: +1; also retained as an operating risk"},
        ),
        (
            "pricing_power",
            {"weight": 1, "rule": "reported pricing power present; strength maps weak/moderate/strong to +1"},
        ),
        (
            "guidance_direction",
            {"weight": 2, "rule": "up/raised: +2; down/cut: -2; maintained/flat: 0"},
        ),
    )
)

_MISSING = object()


def _finite(value: Any) -> float | None:
    """Convert a scalar to a finite float, rejecting booleans and junk."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _record(facts: Any, name: str) -> Mapping[str, Any]:
    metrics = _mapping(facts).get("metrics", {})
    value = metrics.get(name, {}) if isinstance(metrics, Mapping) else {}
    return value if isinstance(value, Mapping) else {"value": value}


def _metric_value(facts: Any, name: str) -> float | None:
    item = _record(facts, name)
    value = item.get("value", _MISSING)
    if value is _MISSING:
        # A few extraction versions emitted a scalar under the metric name.
        return _finite(item) if not isinstance(item, Mapping) else None
    return _finite(value)


def _effective_value(current_facts: Any, name: str, market_inputs: Mapping[str, Any]) -> float | None:
    override = market_inputs.get(name, _MISSING)
    if override is not _MISSING:
        converted = _finite(override)
        if converted is not None:
            return converted
    return _metric_value(current_facts, name)


def _change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    result = current - prior
    return result if math.isfinite(result) else None


def _pct_change(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None or prior == 0:
        return None
    result = (current - prior) / abs(prior) * 100.0
    return result if math.isfinite(result) else None


def _clean_evidence(item: Mapping[str, Any]) -> Any:
    evidence = item.get("evidence", [])
    return evidence if evidence is not None else []


def _metric_output(current_facts: Any, previous_facts: Any, name: str) -> dict[str, Any]:
    current_item = _record(current_facts, name)
    current = _metric_value(current_facts, name)
    prior = _metric_value(previous_facts, name) if previous_facts is not None else None
    return {
        "value": current,
        "unit": current_item.get("unit"),
        "period": current_item.get("period"),
        "evidence": _clean_evidence(current_item),
        "prior_value": prior,
        "change": _change(current, prior),
        "change_pct": _pct_change(current, prior),
    }


def _derived_metric(value: float | None, unit: str, evidence: Any, prior: float | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "period": None,
        "evidence": evidence,
        "prior_value": prior,
        "change": _change(value, prior),
        "change_pct": _pct_change(value, prior),
    }


def _direction_score(change_pct: float | None, weight: int) -> int:
    if change_pct is None:
        return 0
    half = max(1, weight // 2)
    if change_pct >= 10:
        return weight
    if change_pct > 0:
        return half
    if change_pct <= -10:
        return -weight
    return -half


def _ratio(value: float | None, denominator: float | None) -> float | None:
    if value is None or denominator is None or denominator == 0:
        return None
    result = value / denominator * 100.0
    return result if math.isfinite(result) else None


def _qualitative(facts: Any, names: tuple[str, ...]) -> tuple[Any, Any, Any]:
    qualitative = _mapping(facts).get("qualitative", {})
    if not isinstance(qualitative, Mapping):
        return None, None, []
    found: Mapping[str, Any] | None = None
    for wanted in names:
        for key, value in qualitative.items():
            if str(key).strip().lower().replace("-", "_").replace(" ", "_") == wanted:
                found = value if isinstance(value, Mapping) else {"present": value}
                break
        if found is not None:
            break
    if found is None:
        return None, None, []
    present = found.get("present", found.get("value"))
    strength = found.get("strength", found.get("direction"))
    return present, strength, _clean_evidence(found)


def _qual_score(present: Any, strength: Any, *, negative: bool = False) -> int:
    if present is None and strength is None:
        return 0
    if present is False and strength is None:
        return 0
    direction = str(strength).strip().lower() if strength is not None else ""
    if direction in {"negative", "down", "declining", "weakening", "weak", "-1", "-2"}:
        score = -1
    elif direction in {"strong", "high", "positive", "up", "raised", "accelerating", "+2"}:
        score = 1
    elif direction in {"moderate", "medium", "flat", "maintained", "stable", "unchanged", "+1"}:
        score = 1 if direction not in {"flat", "maintained", "stable", "unchanged"} else 0
    else:
        numeric = _finite(strength)
        if numeric is not None:
            score = 1 if numeric > 0 else -1 if numeric < 0 else 0
        else:
            score = 1 if bool(present) else 0
    if isinstance(present, str) and present.strip().lower() in {"false", "no", "absent", "none"}:
        score = 0
    if negative:
        score = -score
    return score


def _signal(rule_name: str, score: int, observed: Any, prior: Any, evidence: Any) -> dict[str, Any]:
    rule = SIGNAL_RULES[rule_name]
    return {
        "rule": rule["rule"],
        "weight": rule["weight"],
        "score": score,
        "observed_value": observed,
        "prior_value": prior,
        "evidence": evidence,
    }


def _infer_growth(current_facts: Any, previous_facts: Any, fcf: float | None, prior_fcf: float | None, revenue_change: float | None) -> float | None:
    fcf_growth = _pct_change(fcf, prior_fcf)
    if fcf_growth is not None:
        return max(-0.20, min(0.20, fcf_growth / 100.0))
    if revenue_change is not None:
        return max(-0.20, min(0.20, revenue_change / 100.0))
    return None


def _rate_input(value: Any, default: float) -> float | None:
    parsed = _finite(default if value is None else value)
    if parsed is not None and parsed > 1:
        parsed /= 100.0
    return parsed


def _valuation(current_facts: Any, previous_facts: Any, market_inputs: Mapping[str, Any], fcf: float | None, prior_fcf: float | None, revenue_change: float | None) -> dict[str, Any]:
    raw_price = _effective_value(current_facts, "market_price", market_inputs)
    price = raw_price if raw_price is not None and raw_price > 0 else None
    raw_shares = _effective_value(current_facts, "shares_outstanding", market_inputs)
    shares = raw_shares if raw_shares is not None and raw_shares > 0 else None
    net_debt = _effective_value(current_facts, "net_debt", market_inputs)
    eps = _metric_value(current_facts, "diluted_eps")
    net_income = _metric_value(current_facts, "net_income")
    market_cap_override = _finite(market_inputs.get("market_cap"))
    market_cap = market_cap_override if market_cap_override is not None else (price * shares if price is not None and shares is not None else None)
    pe = price / eps if price is not None and eps is not None and eps > 0 and price > 0 else None
    pe_method = "price_eps" if pe is not None else None
    if pe is None and market_cap is not None and net_income is not None and net_income > 0:
        pe = market_cap / net_income
        pe_method = "market_cap_net_income"

    wacc = _rate_input(market_inputs.get("discount_rate", market_inputs.get("wacc")), 0.10)
    terminal_growth = _rate_input(market_inputs.get("terminal_growth"), 0.03)
    growth_cap = 0.20
    inferred_growth = _infer_growth(current_facts, previous_facts, fcf, prior_fcf, revenue_change)
    forecast: list[dict[str, Any]] = []
    enterprise_value = None
    terminal_value = None
    present_value_of_terminal = None
    if fcf is None:
        dcf_reason = "starting FCF unavailable"
    elif fcf <= 0:
        dcf_reason = "starting FCF is not positive"
    elif inferred_growth is None:
        dcf_reason = "comparable growth unavailable"
    elif wacc is None or terminal_growth is None or wacc <= terminal_growth or wacc <= 0:
        dcf_reason = "discount rate must exceed terminal growth"
    else:
        dcf_reason = None
    if dcf_reason is None:
        projected = fcf
        present_value = 0.0
        for year in range(1, 6):
            projected *= 1.0 + inferred_growth
            discounted = projected / ((1.0 + wacc) ** year)
            forecast.append({"year": year, "fcf": projected, "present_value": discounted})
            present_value += discounted
        terminal_value = projected * (1.0 + terminal_growth) / (wacc - terminal_growth)
        present_value_of_terminal = terminal_value / ((1.0 + wacc) ** 5)
        enterprise_value = present_value + present_value_of_terminal
    equity_value = enterprise_value - net_debt if enterprise_value is not None and net_debt is not None else None
    per_share = equity_value / shares if equity_value is not None and shares is not None and shares != 0 else None
    if enterprise_value is not None and per_share is None:
        dcf_reason = "net debt and positive shares are required for per-share value"
    assumptions = {
        "forecast_years": 5,
        "wacc": wacc,
        "discount_rate": wacc,
        "terminal_growth": terminal_growth,
        "growth_cap": growth_cap,
        "inferred_growth": inferred_growth,
        "starting_fcf": fcf,
        "net_debt": net_debt,
        "shares_outstanding": shares,
    }
    dcf = {
        "status": (
            "calculated"
            if per_share is not None
            else "enterprise_value_only"
            if enterprise_value is not None
            else "unavailable"
        ),
        "reason": dcf_reason,
        "forecast": forecast,
        "terminal_value": terminal_value,
        "present_value_of_terminal": present_value_of_terminal,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "per_share": per_share,
        "assumptions": assumptions,
    }
    margin_of_safety = 1.0 - price / per_share if price is not None and per_share is not None and per_share > 0 else None
    return {
        "fcf": fcf,
        "pe": pe,
        "pe_ratio": pe,
        "pe_method": pe_method,
        "market_cap": market_cap,
        "market_price": price,
        "dcf": dcf,
        "dcf_per_share": per_share,
        "intrinsic_value": per_share,
        "margin_of_safety": margin_of_safety,
        "assumptions": assumptions,
    }


def _news_crowding(news_items: Any) -> tuple[int, bool]:
    if not isinstance(news_items, (list, tuple)):
        return 0, False
    count = len(news_items)
    # Three or more contemporaneous items is a deliberately simple, auditable
    # proxy for crowded attention; no sentiment is inferred from headlines.
    return count, count >= 3


def _state(score: int, previous_state: Any, prior_analysis_count: Any, valuation: Mapping[str, Any], news_items: Any) -> str:
    if score <= -2:
        return "weakening"
    if score < 2:
        base = "monitor"
    elif score < 5:
        base = "forming"
    elif score < 8:
        base = "confirmed"
    else:
        base = "accelerating"
    previous_mapping = _mapping(previous_state)
    previous = str(previous_mapping.get("stage") or previous_state or "").lower()
    count = _finite(prior_analysis_count) or 0
    _, crowded_news = _news_crowding(news_items)
    pe = _finite(valuation.get("pe"))
    dcf = _mapping(valuation.get("dcf"))
    dcf_per_share = _finite(dcf.get("per_share"))
    market_price = _finite(valuation.get("market_price"))
    valuation_crowded = (pe is not None and pe >= 25) or (
        dcf_per_share is not None
        and dcf_per_share > 0
        and market_price is not None
        and market_price >= dcf_per_share * 1.20
    )
    if score >= 5 and previous in {"confirmed", "accelerating", "mature"} and count >= 2 and valuation_crowded and crowded_news:
        return "mature"
    if previous == "weakening" and score < 2:
        return "weakening"
    return base


def build_deterministic_analysis(
    current_facts: Mapping[str, Any] | None,
    previous_facts: Mapping[str, Any] | None = None,
    market_inputs: Mapping[str, Any] | None = None,
    previous_state: str | None = None,
    prior_analysis_count: int = 0,
    news_items: Any = None,
) -> dict[str, Any]:
    """Build the complete deterministic analysis object from extracted facts."""
    current_facts = current_facts if isinstance(current_facts, Mapping) else {}
    previous_facts = previous_facts if isinstance(previous_facts, Mapping) else None
    market_inputs = market_inputs if isinstance(market_inputs, Mapping) else {}

    metrics: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for name in STANDARD_METRICS:
        metrics[name] = _metric_output(current_facts, previous_facts, name)
    override_units = {
        "market_price": "per share",
        "shares_outstanding": "report scale",
        "net_debt": "report currency",
    }
    for name, fallback_unit in override_units.items():
        override = _finite(market_inputs.get(name))
        if override is None:
            continue
        prior_value = metrics[name]["prior_value"]
        metrics[name] = {
            **metrics[name],
            "value": override,
            "unit": metrics[name]["unit"] or fallback_unit,
            "period": "valuation input",
            "evidence": "manual valuation override",
            "change": _change(override, prior_value),
            "change_pct": _pct_change(override, prior_value),
        }
    ocf = _metric_value(current_facts, "operating_cash_flow")
    capex = _metric_value(current_facts, "capex")
    prior_ocf = _metric_value(previous_facts, "operating_cash_flow") if previous_facts else None
    prior_capex = _metric_value(previous_facts, "capex") if previous_facts else None
    ocf_unit = str(_record(current_facts, "operating_cash_flow").get("unit") or "")
    capex_unit = str(_record(current_facts, "capex").get("unit") or "")
    prior_ocf_unit = str(_record(previous_facts, "operating_cash_flow").get("unit") or "") if previous_facts else ""
    prior_capex_unit = str(_record(previous_facts, "capex").get("unit") or "") if previous_facts else ""
    current_units_match = not ocf_unit or not capex_unit or ocf_unit.casefold() == capex_unit.casefold()
    prior_units_match = not prior_ocf_unit or not prior_capex_unit or prior_ocf_unit.casefold() == prior_capex_unit.casefold()
    fcf = ocf - capex if current_units_match and ocf is not None and capex is not None else None
    prior_fcf = prior_ocf - prior_capex if prior_units_match and prior_ocf is not None and prior_capex is not None else None
    revenue = _metric_value(current_facts, "revenue")
    prior_revenue = _metric_value(previous_facts, "revenue") if previous_facts else None
    fcf_margin = fcf / revenue * 100.0 if fcf is not None and revenue not in (None, 0) else None
    prior_fcf_margin = prior_fcf / prior_revenue * 100.0 if prior_fcf is not None and prior_revenue not in (None, 0) else None
    metrics["fcf"] = _derived_metric(fcf, ocf_unit or capex_unit or "currency", [metrics["operating_cash_flow"]["evidence"], metrics["capex"]["evidence"]], prior_fcf)
    metrics["free_cash_flow"] = metrics["fcf"].copy()
    metrics["fcf_margin"] = _derived_metric(fcf_margin, "percent", metrics["fcf"]["evidence"], prior_fcf_margin)

    signals: OrderedDict[str, dict[str, Any]] = OrderedDict()
    revenue_change = metrics["revenue"]["change_pct"]
    signals["revenue"] = _signal("revenue", _direction_score(revenue_change, 2), revenue, prior_revenue, metrics["revenue"]["evidence"])

    capex_change = metrics["capex"]["change_pct"]
    signals["capex"] = _signal(
        "capex",
        _direction_score(capex_change, 2),
        capex,
        prior_capex,
        metrics["capex"]["evidence"],
    )

    backlog = _metric_value(current_facts, "backlog")
    prior_backlog = _metric_value(previous_facts, "backlog") if previous_facts else None
    signals["backlog"] = _signal("backlog", _direction_score(_pct_change(backlog, prior_backlog), 1), backlog, prior_backlog, metrics["backlog"]["evidence"])

    inventory = _metric_value(current_facts, "inventory")
    prior_inventory = _metric_value(previous_facts, "inventory") if previous_facts else None
    inventory_growth = _pct_change(inventory, prior_inventory)
    revenue_growth = _pct_change(revenue, prior_revenue)
    inventory_gap = _change(inventory_growth, revenue_growth)
    inventory_score = 0 if inventory_gap is None else (-2 if inventory_gap > 5 else -1 if inventory_gap > 0 else 1)
    signals["inventory_vs_revenue"] = _signal("inventory_vs_revenue", inventory_score, inventory, prior_inventory, metrics["inventory"]["evidence"])

    fcf_score = _direction_score(_pct_change(fcf, prior_fcf), 2)
    if fcf_score == 0 and fcf is not None:
        fcf_score = 2 if fcf > 0 else -2 if fcf < 0 else 0
    signals["fcf"] = _signal("fcf", fcf_score, fcf, prior_fcf, metrics["fcf"]["evidence"])

    margin = _metric_value(current_facts, "gross_margin")
    prior_margin = _metric_value(previous_facts, "gross_margin") if previous_facts else None
    margin_delta = _change(margin, prior_margin)
    margin_threshold = 1.0 if (margin is not None and abs(margin) > 1) or (prior_margin is not None and abs(prior_margin) > 1) else 0.02
    margin_score = 0 if margin_delta is None else 2 if margin_delta >= margin_threshold else -2 if margin_delta <= -margin_threshold else 1 if margin_delta > 0 else -1
    signals["gross_margin_delta"] = _signal("gross_margin_delta", margin_score, margin, prior_margin, metrics["gross_margin"]["evidence"])

    qualitative_specs = (
        ("ai_demand", ("ai_demand", "ai"), False),
        ("data_centre_demand", ("data_centre_demand", "data_center_demand", "datacenter_demand", "datacentre_demand"), False),
        ("supply_constraints", ("supply_constraints", "supply_constraint"), False),
        ("pricing_power", ("pricing_power",), False),
    )
    for name, aliases, negative in qualitative_specs:
        present, strength, evidence = _qualitative(current_facts, aliases)
        prior_present, prior_strength, _ = _qualitative(previous_facts, aliases) if previous_facts else (None, None, [])
        observed = present if present is not None else strength
        prior_observed = prior_present if prior_present is not None else prior_strength
        signals[name] = _signal(name, _qual_score(present, strength, negative=negative), observed, prior_observed, evidence)

    guidance_up, _, guidance_up_evidence = _qualitative(
        current_facts, ("guidance_up",)
    )
    guidance_down, _, guidance_down_evidence = _qualitative(
        current_facts, ("guidance_down",)
    )
    direction = "up" if guidance_up else "down" if guidance_down else None
    if direction is None:
        legacy_direction, legacy_strength, legacy_evidence = _qualitative(
            current_facts, ("guidance_direction", "guidance")
        )
        direction = legacy_direction if legacy_direction is not None else legacy_strength
        evidence = legacy_evidence
    else:
        evidence = guidance_up_evidence if direction == "up" else guidance_down_evidence
    direction_text = str(direction or "").lower()
    guidance_score = 2 if direction_text in {"up", "raised", "raise", "positive", "higher"} else -2 if direction_text in {"down", "cut", "lower", "negative", "reduced"} else 0
    signals["guidance_direction"] = _signal(
        "guidance_direction", guidance_score, direction, None, evidence
    )

    score = sum(item["score"] for item in signals.values())
    valuation = _valuation(current_facts, previous_facts, market_inputs, fcf, prior_fcf, revenue_change)
    currency_unit = str(metrics["fcf"]["unit"] or "")
    currency_code = next(
        (code for code in CURRENCY_CODES if currency_unit.upper().startswith(code)),
        "",
    )
    valuation["currency_unit"] = currency_unit
    valuation["per_share_unit"] = f"{currency_code}/share" if currency_code else None
    valuation["dcf"]["unit"] = currency_unit
    state = _state(score, previous_state, prior_analysis_count, valuation, news_items)
    previous_stage = (
        _mapping(previous_state).get("stage")
        if isinstance(previous_state, Mapping)
        else previous_state
    )
    transition = (
        "initial"
        if not previous_stage
        else "unchanged"
        if str(previous_stage).lower() == state
        else f"{str(previous_stage).lower()} -> {state}"
    )

    drivers: list[str] = []
    risks: list[str] = []
    watch_items: list[str] = []
    labels = {
        "revenue": "revenue growth",
        "capex": "capital investment",
        "backlog": "backlog",
        "inventory_vs_revenue": "inventory relative to revenue",
        "fcf": "free cash flow",
        "gross_margin_delta": "gross margin",
        "ai_demand": "AI demand",
        "data_centre_demand": "data-centre demand",
        "supply_constraints": "supply constraints",
        "pricing_power": "pricing power",
        "guidance_direction": "guidance",
    }
    for name, item in signals.items():
        label = labels[name]
        if item["score"] > 0:
            drivers.append(label)
        elif item["score"] < 0:
            risks.append(label)
        if item["observed_value"] is None:
            watch_items.append(f"{label}: missing comparable evidence")
    supply_signal = signals["supply_constraints"]
    if supply_signal["score"] > 0 and "supply constraints" not in risks:
        risks.append("supply constraints")
    if valuation["pe"] is None:
        watch_items.append("valuation: P/E unavailable")
    if _mapping(valuation["dcf"]).get("per_share") is None:
        watch_items.append("valuation: DCF per-share value unavailable")
    news_count, crowded = _news_crowding(news_items)
    if crowded:
        watch_items.append(f"news attention: {news_count} items may indicate crowded expectations")

    return {
        "metrics": metrics,
        "valuation": valuation,
        "signals": signals,
        "score": score,
        "state": {
            "stage": state,
            "previous_stage": previous_stage,
            "score": score,
            "transition": transition,
            "rule_version": "1",
        },
        "drivers": drivers,
        "risks": risks,
        "watch_items": watch_items,
    }
