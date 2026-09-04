"""Pure, deterministic investment-report analysis rules.

No I/O. Model output is treated as untrusted input: only finite numeric values
are allowed into arithmetic, and missing inputs remain explicit None values.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "CNY", "KRW", "TWD", "HKD", "SGD", "INR"}
MATERIAL_RELATIONSHIP_KINDS = {
    "same_period_top_bottom_growth",
    "external_effect_on_recipient",
    "cash_generation_vs_investment",
}
MAX_MATERIAL_RELATIONSHIPS = 8
MAX_RELATIONSHIP_FACT_REFS = 8
MAX_NORMALIZED_RELATIONSHIP_FACTS = 24

STANDARD_METRICS = {
    "revenue",
    "operating_cash_flow",
    "capex",
    "fcf",
    "fcf_margin",
    "gross_margin",
    "gross_profit",
    "net_income",
    "diluted_eps",
    "shares_outstanding",
    "net_debt",
    "ebitda",
    "inventory",
    "backlog",
    "total_debt",
    "total_assets",
    "total_liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
}

SIGNAL_RULES = OrderedDict(
    {
        "revenue": {
            "weight": 2,
            "rule": "change >= 10%: +2; change > 0%: +1; change < -10%: -2; otherwise -1",
        },
        "capex": {
            "weight": 2,
            "rule": "capex growth >= 15%: +2; growth > 0%: +1; decline < -10%: -2; otherwise -1",
        },
        "fcf": {
            "weight": 2,
            "rule": "fcf margin expansion: +2; growth: +1; margin contraction: -2; decline: -1",
        },
        "gross_margin_delta": {
            "weight": 1,
            "rule": "gross margin >= 60% or expansion: +1; contraction: -1",
        },
        "inventory_vs_revenue": {
            "weight": 2,
            "rule": "inventory growth > revenue growth + 10%: -2; in line: +1",
        },
        "backlog": {"weight": 1, "rule": "backlog growth > 0%: +1; decline: -1"},
        "ai_demand": {
            "weight": 2,
            "rule": "qualitative ai demand: strong=+2, moderate=+1",
        },
        "data_centre_demand": {
            "weight": 1,
            "rule": "qualitative datacenter demand: strong/moderate=+1",
        },
        "supply_constraints": {
            "weight": 1,
            "rule": "tight supply / structural shortages: +1",
        },
        "pricing_power": {"weight": 1, "rule": "pricing power / rising ASPs: +1"},
        "guidance_direction": {
            "weight": 2,
            "rule": "raised guidance: +2; in-line: +1; cut: -2; maintained/flat: 0",
        },
    }
)

_METRIC_ALIASES = {
    "cash_paid_for_property_and_equipment": "capex",
    "capital_expenditures": "capex",
    "gross_margin_dollars": "gross_profit",
}


def _finite(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _dec(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        d = Decimal(str(val))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _plain_num(val: Decimal | float) -> int | float:
    if isinstance(val, Decimal):
        return int(val) if val == val.to_integral_value() else float(val)
    return int(val) if float(val).is_integer() else float(val)


def _mapping(val: Any) -> Mapping[str, Any]:
    return val if isinstance(val, Mapping) else {}


def _pct_change(curr: float | None, prior: float | None) -> float | None:
    if curr is None or prior is None or prior == 0:
        return None
    return ((curr - prior) / abs(prior)) * 100.0


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _clean_evidence(item: Mapping[str, Any]) -> list[Any]:
    ev = item.get("evidence")
    if isinstance(ev, (list, tuple)):
        return [e for e in ev if isinstance(e, (str, dict))]
    if isinstance(ev, str) and ev:
        return [ev]
    return []


def _extract_metric(
    facts: Mapping[str, Any], name: str
) -> tuple[float | None, dict[str, Any]]:
    metrics = _mapping(facts.get("metrics"))
    rec = metrics.get(name)
    if not isinstance(rec, Mapping):
        alias = _METRIC_ALIASES.get(name)
        if alias:
            rec = metrics.get(alias)
    if not isinstance(rec, Mapping) and name in _METRIC_ALIASES.values():
        for k, v in _METRIC_ALIASES.items():
            if v == name and k in metrics and isinstance(metrics[k], Mapping):
                rec = metrics[k]
                break
    if isinstance(rec, Mapping):
        val = _finite(rec.get("value"))
        return val, dict(rec)
    val = _finite(facts.get(name))
    return val, {"value": val, "unit": "currency", "evidence": []}


def _signal(
    rule_name: str,
    score: int,
    observed: Any,
    prior: Any,
    evidence: Any,
    *,
    basis: str = "deterministic_metric",
    comparable: bool | None = None,
) -> dict[str, Any]:
    obs_n = _finite(observed) if basis == "deterministic_metric" else None
    pri_n = _finite(prior) if basis == "deterministic_metric" else None
    if comparable is None:
        comparable = (
            (obs_n is not None and pri_n is not None)
            if basis == "deterministic_metric"
            else False
        )
    chg = (obs_n - pri_n) if obs_n is not None and pri_n is not None else None
    chg_pct = _pct_change(obs_n, pri_n)
    ev_list = (
        [evidence]
        if isinstance(evidence, (str, dict))
        else (list(evidence) if isinstance(evidence, (list, tuple)) else [])
    )
    return {
        "rule": SIGNAL_RULES.get(rule_name, {}).get("rule", ""),
        "score": score,
        "weight": SIGNAL_RULES.get(rule_name, {}).get("weight", 1),
        "basis": basis,
        "observed_value": obs_n if basis == "deterministic_metric" else observed,
        "prior_value": pri_n if basis == "deterministic_metric" else prior,
        "change": chg,
        "change_pct": chg_pct,
        "comparable": comparable,
        "evidence": ev_list,
    }


def _dcf(
    fcf: float | None, growth: float | None, dr: float, tg: float
) -> dict[str, Any] | None:
    if fcf is None or fcf <= 0 or growth is None or dr <= tg or dr <= 0 or tg < 0:
        return None
    proj_fcf = fcf
    pv_forecast = 0.0
    forecast = []
    for yr in range(1, 6):
        proj_fcf *= 1.0 + growth
        pv = proj_fcf / ((1.0 + dr) ** yr)
        pv_forecast += pv
        forecast.append({"year": yr, "fcf": proj_fcf, "present_value": pv})
    term_fcf = proj_fcf * (1.0 + tg)
    term_val = term_fcf / (dr - tg)
    pv_term = term_val / ((1.0 + dr) ** 5)
    ev = pv_forecast + pv_term
    return {
        "enterprise_value": ev,
        "pv_forecast": pv_forecast,
        "pv_terminal": pv_term,
        "terminal_value": term_val,
        "forecast": forecast,
        "assumptions": {
            "discount_rate": dr,
            "terminal_growth": tg,
            "inferred_growth": growth,
        },
    }


def _dcf_case(
    fcf: float | None,
    growth: float,
    dr: float,
    tg: float,
    net_debt: float | None,
    shares: float | None,
) -> dict[str, Any] | None:
    d = _dcf(fcf, growth, dr, tg)
    if not d:
        return None
    ev = d["enterprise_value"]
    eq_val = (ev - net_debt) if (net_debt is not None) else None
    ps = (eq_val / shares) if (eq_val is not None and shares and shares > 0) else None
    return {
        "enterprise_value": ev,
        "equity_value": eq_val,
        "per_share": ps,
        "assumptions": d["assumptions"],
    }


def _valuation_sensitivity(
    base_fcf: float | None,
    base_g: float | None,
    base_dr: float,
    base_tg: float,
    net_debt: float | None,
    shares: float | None,
) -> dict[str, Any]:
    if base_fcf is None or base_fcf <= 0 or base_g is None or base_dr <= base_tg:
        return {
            "status": "unavailable",
            "reason": "positive base cash flow and valid assumptions required",
            "wacc_terminal_grid": [],
            "drivers": {},
            "range": {
                "enterprise_value_min": None,
                "enterprise_value_max": None,
                "per_share_min": None,
                "per_share_max": None,
            },
            "largest_range_driver": None,
        }

    grid = []
    ev_all, ps_all = [], []
    for dr in [base_dr - 0.01, base_dr, base_dr + 0.01]:
        for tg in [base_tg - 0.005, base_tg, base_tg + 0.005]:
            if dr > tg:
                c = _dcf_case(base_fcf, base_g, dr, tg, net_debt, shares)
                if c:
                    ev_all.append(c["enterprise_value"])
                    if c["per_share"] is not None:
                        ps_all.append(c["per_share"])
                    grid.append(
                        {
                            "discount_rate": dr,
                            "terminal_growth": tg,
                            "enterprise_value": c["enterprise_value"],
                            "per_share": c["per_share"],
                        }
                    )

    drivers = {}
    spreads = {}
    param_ranges = {
        "starting_fcf": [
            (base_fcf * 0.9, base_g, base_dr, base_tg),
            (base_fcf * 1.1, base_g, base_dr, base_tg),
        ],
        "annual_growth": [
            (base_fcf, base_g * 0.8, base_dr, base_tg),
            (base_fcf, base_g * 1.2, base_dr, base_tg),
        ],
        "discount_rate": [
            (base_fcf, base_g, base_dr + 0.01, base_tg),
            (base_fcf, base_g, max(0.001, base_dr - 0.01), base_tg),
        ],
        "terminal_growth": [
            (base_fcf, base_g, base_dr, max(0.0, base_tg - 0.005)),
            (base_fcf, base_g, base_dr, base_tg + 0.005),
        ],
    }
    for pname, (low_p, high_p) in param_ranges.items():
        c_low = _dcf_case(*low_p, net_debt, shares)
        c_high = _dcf_case(*high_p, net_debt, shares)
        if c_low and c_high:
            spread = abs(c_high["enterprise_value"] - c_low["enterprise_value"])
            spreads[pname] = spread
            drivers[pname] = {
                "enterprise_value_low": min(
                    c_low["enterprise_value"], c_high["enterprise_value"]
                ),
                "enterprise_value_high": max(
                    c_low["enterprise_value"], c_high["enterprise_value"]
                ),
                "per_share_low": min(c_low["per_share"], c_high["per_share"])
                if c_low["per_share"] and c_high["per_share"]
                else None,
                "per_share_high": max(c_low["per_share"], c_high["per_share"])
                if c_low["per_share"] and c_high["per_share"]
                else None,
            }

    largest = max(spreads, key=spreads.get) if spreads else None
    has_ps = bool(ps_all)
    return {
        "status": "calculated" if has_ps else "enterprise_value_only",
        "reason": None
        if has_ps
        else "net debt and positive shares are required for per-share sensitivity",
        "wacc_terminal_grid": grid,
        "drivers": drivers,
        "range": {
            "enterprise_value_min": min(ev_all) if ev_all else None,
            "enterprise_value_max": max(ev_all) if ev_all else None,
            "per_share_min": min(ps_all) if ps_all else None,
            "per_share_max": max(ps_all) if ps_all else None,
        },
        "largest_range_driver": largest,
    }


@dataclass(frozen=True, slots=True)
class NormalizedRelationshipFact:
    fact_id: str
    metric_key: str
    metric_label: str
    value: Decimal
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    scope: str = "consolidated"
    comparison_basis: str = "none"
    temporal_basis: str = "period_flow"
    cash_basis: str = "not_applicable"
    source_paths: tuple[str, ...] = ()
    derivation: str | None = None
    qualifiers: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "metric_key": self.metric_key,
            "metric_label": self.metric_label,
            "value": _plain_num(self.value),
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "scope": self.scope,
            "comparison_basis": self.comparison_basis,
            "temporal_basis": self.temporal_basis,
            "cash_basis": self.cash_basis,
            "source_paths": list(self.source_paths),
            "derivation": self.derivation,
            "qualifiers": list(self.qualifiers),
        }


@dataclass(frozen=True, slots=True)
class RelationshipFactRef:
    fact_id: str
    role: str
    leaf: str

    def to_payload(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "role": self.role, "leaf": self.leaf}


@dataclass(frozen=True, slots=True)
class MaterialRelationship:
    relationship_id: str
    kind: str
    priority: int
    required_fact_refs: tuple[RelationshipFactRef, ...]
    material_relationships: tuple[Any, ...] = ()
    compatibility: str = "compatible"

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "kind": self.kind,
            "priority": self.priority,
            "compatibility": self.compatibility,
            "required_facts": [
                {
                    "fact_path": f"deterministic_current.relationship_facts.{ref.fact_id}",
                    "role": ref.role,
                    "leaf": ref.leaf,
                }
                for ref in self.required_fact_refs
            ],
            "required_fact_refs": [ref.to_payload() for ref in self.required_fact_refs],
            "material_relationships": [
                r.to_payload() for r in self.material_relationships
            ],
        }


@dataclass(frozen=True, slots=True)
class MaterialRelationshipContract:
    relationship_facts: tuple[NormalizedRelationshipFact, ...]
    material_relationships: tuple[MaterialRelationship, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship_facts": {
                f.fact_id: f.to_payload() for f in self.relationship_facts
            },
            "material_relationships": [
                r.to_payload() for r in self.material_relationships
            ],
        }


def build_material_relationship_contract(
    current_facts: Mapping[str, Any], prior_facts: Mapping[str, Any] | None = None
) -> MaterialRelationshipContract:
    raw_curr = _mapping(current_facts)
    curr_m = (
        raw_curr.get("metrics")
        if isinstance(raw_curr.get("metrics"), Mapping)
        else raw_curr
    )
    facts_dict: dict[str, NormalizedRelationshipFact] = {}
    relationships: list[MaterialRelationship] = []

    def get_or_add_fact(
        key: str, rec: Mapping[str, Any], default_period: str = "FY2025"
    ) -> NormalizedRelationshipFact | None:
        val = _dec(rec.get("value"))
        if val is None:
            return None
        period = str(rec.get("period") or default_period)
        fid = f"{key}:{period}:{_plain_num(val)}"
        if fid not in facts_dict:
            tags = _mapping(rec.get("relationship_tags"))
            facts_dict[fid] = NormalizedRelationshipFact(
                fact_id=fid,
                metric_key=key,
                metric_label=key.replace("_", " ").title(),
                value=val,
                unit=rec.get("unit"),
                currency=rec.get("currency"),
                period=period,
                scope=tags.get("scope", "consolidated"),
                comparison_basis=tags.get("comparison_basis", "none"),
                temporal_basis=tags.get("temporal_basis", "period_flow"),
                cash_basis=tags.get("cash_basis", "not_applicable"),
                source_paths=(str(rec.get("source") or "reported"),),
            )
        return facts_dict[fid]

    for k, rec in curr_m.items():
        if isinstance(rec, Mapping) and _dec(rec.get("value")) is not None:
            get_or_add_fact(k, rec)

    # 1. same_period_top_bottom_growth
    rev_g = curr_m.get("revenue_growth") or curr_m.get("revenue")
    ni_g = curr_m.get("net_income_growth") or curr_m.get("net_income")
    if isinstance(rev_g, Mapping) and isinstance(ni_g, Mapping):
        f_rev = get_or_add_fact("revenue", rev_g)
        f_ni = get_or_add_fact("net_income", ni_g)
        if f_rev and f_ni:
            relationships.append(
                MaterialRelationship(
                    relationship_id=f"rel:top_bottom:{f_rev.fact_id}:{f_ni.fact_id}",
                    kind="same_period_top_bottom_growth",
                    priority=1,
                    required_fact_refs=(
                        RelationshipFactRef(f_rev.fact_id, "top_line", "growth"),
                        RelationshipFactRef(f_ni.fact_id, "bottom_line", "growth"),
                    ),
                )
            )

    # 2. cash_generation_vs_investment
    ocf = curr_m.get("operating_cash_flow")
    cpx = curr_m.get("capex") or curr_m.get("cash_paid_for_property_and_equipment")
    if isinstance(ocf, Mapping) and isinstance(cpx, Mapping):
        f_ocf = get_or_add_fact("operating_cash_flow", ocf)
        f_cpx = get_or_add_fact("capex", cpx)
        if f_ocf and f_cpx:
            relationships.append(
                MaterialRelationship(
                    relationship_id=f"rel:cash_gen:{f_ocf.fact_id}:{f_cpx.fact_id}",
                    kind="cash_generation_vs_investment",
                    priority=2,
                    required_fact_refs=(
                        RelationshipFactRef(f_ocf.fact_id, "operating_cash", "flow"),
                        RelationshipFactRef(
                            f_cpx.fact_id, "capital_expenditure", "flow"
                        ),
                    ),
                )
            )

    # 3. external_effect_on_recipient
    for k, rec in curr_m.items():
        if isinstance(rec, Mapping):
            tags = _mapping(rec.get("relationship_tags"))
            if tags.get("role") == "external_effect":
                f_ext = get_or_add_fact(k, rec)
                if f_ext:
                    relationships.append(
                        MaterialRelationship(
                            relationship_id=f"rel:ext:{f_ext.fact_id}",
                            kind="external_effect_on_recipient",
                            priority=3,
                            required_fact_refs=(
                                RelationshipFactRef(
                                    f_ext.fact_id,
                                    "external_effect",
                                    tags.get("leaf", "effect"),
                                ),
                            ),
                        )
                    )

    return MaterialRelationshipContract(
        relationship_facts=tuple(facts_dict.values())[
            :MAX_NORMALIZED_RELATIONSHIP_FACTS
        ],
        material_relationships=tuple(relationships)[:MAX_MATERIAL_RELATIONSHIPS],
    )


def build_deterministic_analysis(
    current_facts: Any,
    previous_facts: Any = None,
    market_inputs: Mapping[str, Any] | None = None,
    *,
    previous_state: Any = None,
    prior_analysis_count: Any = None,
    news_items: Any = None,
) -> dict[str, Any]:
    curr = _mapping(current_facts)
    prior = _mapping(previous_facts)
    mkt = _mapping(market_inputs)

    # 1. Metrics extraction & standardization
    metrics: OrderedDict[str, dict[str, Any]] = OrderedDict()
    curr_m = _mapping(curr.get("metrics"))
    prior_m = _mapping(prior.get("metrics"))

    all_keys = list(STANDARD_METRICS)
    for k in list(curr_m.keys()) + list(prior_m.keys()) + list(mkt.keys()):
        if k not in all_keys and not k.startswith("_"):
            all_keys.append(k)

    rev, rev_rec = _extract_metric(curr, "revenue")
    pri_rev, _ = _extract_metric(prior, "revenue")
    ocf, ocf_rec = _extract_metric(curr, "operating_cash_flow")
    pri_ocf, _ = _extract_metric(prior, "operating_cash_flow")
    cpx, cpx_rec = _extract_metric(curr, "capex")
    pri_cpx, _ = _extract_metric(prior, "capex")

    explicit_fcf, fcf_rec = _extract_metric(curr, "fcf")
    pri_explicit_fcf, _ = _extract_metric(prior, "fcf")
    if explicit_fcf is None:
        explicit_fcf, fcf_rec = _extract_metric(curr, "free_cash_flow")
        pri_explicit_fcf, _ = _extract_metric(prior, "free_cash_flow")

    calc_fcf = (ocf - cpx) if (ocf is not None and cpx is not None) else None
    pri_calc_fcf = (
        (pri_ocf - pri_cpx) if (pri_ocf is not None and pri_cpx is not None) else None
    )
    fcf = explicit_fcf if explicit_fcf is not None else calc_fcf
    pri_fcf = pri_explicit_fcf if pri_explicit_fcf is not None else pri_calc_fcf

    for k in all_keys:
        if k == "fcf":
            c_val = fcf
            p_val = pri_fcf
            src = fcf_rec.get("source") or (
                "reported" if explicit_fcf is not None else "derived"
            )
            rec = fcf_rec
        elif k == "fcf_margin":
            c_val = (
                ((fcf / rev) * 100.0) if (fcf is not None and rev and rev > 0) else None
            )
            p_val = (
                ((pri_fcf / pri_rev) * 100.0)
                if (pri_fcf is not None and pri_rev and pri_rev > 0)
                else None
            )
            src = "derived"
            rec = {}
        elif k == "gross_margin":
            gp, _ = _extract_metric(curr, "gross_profit")
            c_val, rec = _extract_metric(curr, "gross_margin")
            if c_val is None and gp is not None and rev and rev > 0:
                c_val = (gp / rev) * 100.0
            pri_gp, _ = _extract_metric(prior, "gross_profit")
            p_val, _ = _extract_metric(prior, "gross_margin")
            if p_val is None and pri_gp is not None and pri_rev and pri_rev > 0:
                p_val = (pri_gp / pri_rev) * 100.0
            src = rec.get("source", "reported")
        else:
            override = _finite(mkt.get(k))
            c_val, rec = _extract_metric(curr, k)
            if override is not None:
                c_val = override
            p_val, _ = _extract_metric(prior, k)
            src = rec.get("source", "reported")

        if c_val is not None or p_val is not None or k in STANDARD_METRICS:
            chg = (c_val - p_val) if (c_val is not None and p_val is not None) else None
            chg_pct = _pct_change(c_val, p_val)
            entry: dict[str, Any] = {
                "value": c_val,
                "prior_value": p_val,
                "change": chg,
                "pct_change": chg_pct,
                "unit": rec.get(
                    "unit", "percent" if "margin" in k or "growth" in k else "currency"
                ),
                "period": rec.get("period", "FY2025"),
                "evidence": _clean_evidence(rec),
                "source": src,
            }
            if "concept" in rec:
                entry["concept"] = rec["concept"]
            if "currency" in rec:
                entry["currency"] = rec["currency"]
            metrics[k] = entry

    # 2. Valuation
    price = _finite(mkt.get("market_price")) or _finite(
        curr_m.get("market_price", {}).get("value")
    )
    shares = _finite(mkt.get("shares_outstanding")) or _finite(
        curr_m.get("shares_outstanding", {}).get("value")
    )
    net_debt = _finite(mkt.get("net_debt")) or _finite(
        curr_m.get("net_debt", {}).get("value")
    )
    mcap_override = _finite(mkt.get("market_cap"))
    mcap = (
        mcap_override
        if mcap_override is not None
        else ((price * shares) if (price and shares) else None)
    )
    eps = _finite(curr_m.get("diluted_eps", {}).get("value"))
    ni = _finite(curr_m.get("net_income", {}).get("value"))
    ebitda = _finite(curr_m.get("ebitda", {}).get("value"))
    ev = (mcap + net_debt) if (mcap is not None and net_debt is not None) else None

    pe = (
        (price / eps)
        if (price and eps and eps > 0)
        else ((mcap / ni) if (mcap and ni and ni > 0) else None)
    )
    pfcf = (mcap / fcf) if (mcap and fcf and fcf > 0) else None
    ev_ebitda = (ev / ebitda) if (ev and ebitda and ebitda > 0) else None
    ev_revenue = (ev / rev) if (ev and rev and rev > 0) else None

    dr_in = _finite(mkt.get("discount_rate")) or 0.10
    dr = (dr_in / 100.0) if dr_in > 1.0 else dr_in
    tg_in = _finite(mkt.get("terminal_growth")) or 0.03
    tg = (tg_in / 100.0) if tg_in > 1.0 else tg_in

    inferred_g = (
        min(0.20, max(0.0, ((fcf - pri_fcf) / abs(pri_fcf))))
        if (fcf and pri_fcf and pri_fcf > 0)
        else 0.05
    )
    dcf_base = _dcf(fcf, inferred_g, dr, tg)
    dcf_ev = dcf_base["enterprise_value"] if dcf_base else None
    dcf_eq = (
        (dcf_ev - net_debt) if (dcf_ev is not None and net_debt is not None) else None
    )
    dcf_ps = (
        (dcf_eq / shares) if (dcf_eq is not None and shares and shares > 0) else None
    )

    bull = _dcf_case(
        fcf,
        min(0.25, inferred_g * 1.2),
        max(0.06, dr - 0.01),
        tg + 0.005,
        net_debt,
        shares,
    )
    bear = _dcf_case(
        fcf,
        max(0.0, inferred_g * 0.8),
        dr + 0.01,
        max(0.01, tg - 0.005),
        net_debt,
        shares,
    )
    sensitivity = _valuation_sensitivity(fcf, inferred_g, dr, tg, net_debt, shares)
    dcf_assumptions = {
        "discount_rate": dr,
        "terminal_growth": tg,
        "inferred_growth": inferred_g,
        "shares_outstanding": shares,
        "net_debt": net_debt,
    }
    dcf_status = (
        "calculated"
        if dcf_ps is not None
        else ("enterprise_value_only" if dcf_ev is not None else "unavailable")
    )

    valuation = {
        "market_price": price,
        "market_cap": mcap,
        "enterprise_value": ev,
        "pe": pe,
        "pe_ratio": pe,
        "pfcf": pfcf,
        "ev_ebitda": ev_ebitda,
        "ev_revenue": ev_revenue,
        "dcf": {
            "status": dcf_status,
            "enterprise_value": dcf_ev,
            "equity_value": dcf_eq,
            "per_share": dcf_ps,
            "pv_forecast": dcf_base["pv_forecast"] if dcf_base else None,
            "pv_terminal": dcf_base["pv_terminal"] if dcf_base else None,
            "terminal_value": dcf_base["terminal_value"] if dcf_base else None,
            "forecast": dcf_base["forecast"] if dcf_base else [],
            "assumptions": dcf_assumptions,
            "sensitivity": sensitivity,
        },
        "base": {
            "enterprise_value": dcf_ev,
            "equity_value": dcf_eq,
            "per_share": dcf_ps,
            "assumptions": dcf_assumptions,
        },
        "bull": bull
        or {
            "enterprise_value": None,
            "equity_value": None,
            "per_share": None,
            "assumptions": {},
        },
        "bear": bear
        or {
            "enterprise_value": None,
            "equity_value": None,
            "per_share": None,
            "assumptions": {},
        },
        "assumptions": dcf_assumptions,
    }

    # 3. Fundamentals
    eq = _finite(curr_m.get("equity", {}).get("value"))
    ta = _finite(curr_m.get("total_assets", {}).get("value"))
    t_debt = _finite(curr_m.get("total_debt", {}).get("value"))
    ca = _finite(curr_m.get("current_assets", {}).get("value"))
    cl = _finite(curr_m.get("current_liabilities", {}).get("value"))
    roe = _ratio(ni, eq)
    roa = _ratio(ni, ta)
    nm = _ratio(ni, rev)
    fundamentals = {
        "roe": roe,
        "roa": roa,
        "debt_to_equity": _ratio(t_debt, eq),
        "current_ratio": _ratio(ca, cl),
        "fcf_conversion": _ratio(fcf, ni),
        "net_margin_pct": (nm * 100.0) if nm is not None else None,
        "return_on_equity_pct": (roe * 100.0) if roe is not None else None,
        "return_on_assets_pct": (roa * 100.0) if roa is not None else None,
    }

    # 4. Signals
    signals: OrderedDict[str, dict[str, Any]] = OrderedDict()
    qual = _mapping(curr.get("qualitative"))

    # Revenue signal
    rev_pct = _pct_change(rev, pri_rev)
    rev_score = (
        2
        if (rev_pct is not None and rev_pct >= 10)
        else (
            1
            if (rev_pct is not None and rev_pct > 0)
            else (
                -2
                if (rev_pct is not None and rev_pct < -10)
                else (0 if rev_pct is None else -1)
            )
        )
    )
    signals["revenue"] = _signal(
        "revenue", rev_score, rev, pri_rev, rev_rec.get("evidence", [])
    )

    # Capex signal
    cpx_pct = _pct_change(cpx, pri_cpx)
    cpx_score = (
        2
        if (cpx_pct is not None and cpx_pct >= 15)
        else (
            1
            if (cpx_pct is not None and cpx_pct > 0)
            else (
                -2
                if (cpx_pct is not None and cpx_pct < -10)
                else (0 if cpx_pct is None else -1)
            )
        )
    )
    signals["capex"] = _signal(
        "capex", cpx_score, cpx, pri_cpx, cpx_rec.get("evidence", [])
    )

    # FCF signal
    fcf_m_curr = metrics["fcf_margin"]["value"]
    fcf_m_pri = metrics["fcf_margin"]["prior_value"]
    fcf_score = (
        2
        if (fcf_m_curr is not None and fcf_m_pri is not None and fcf_m_curr > fcf_m_pri)
        else (
            1
            if (fcf and pri_fcf and fcf > pri_fcf)
            else (
                -2
                if (
                    fcf_m_curr is not None
                    and fcf_m_pri is not None
                    and fcf_m_curr < fcf_m_pri
                )
                else 0
            )
        )
    )
    signals["fcf"] = _signal(
        "fcf", fcf_score, fcf, pri_fcf, fcf_rec.get("evidence", [])
    )

    # Gross margin delta
    gm_curr = metrics["gross_margin"]["value"]
    gm_pri = metrics["gross_margin"]["prior_value"]
    gm_score = (
        1
        if (
            gm_curr is not None
            and (gm_curr >= 60 or (gm_pri is not None and gm_curr > gm_pri))
        )
        else (
            -1
            if (gm_curr is not None and gm_pri is not None and gm_curr < gm_pri)
            else 0
        )
    )
    signals["gross_margin_delta"] = _signal(
        "gross_margin_delta",
        gm_score,
        gm_curr,
        gm_pri,
        metrics["gross_margin"]["evidence"],
    )

    # Inventory vs revenue
    inv, inv_rec = _extract_metric(curr, "inventory")
    pri_inv, _ = _extract_metric(prior, "inventory")
    inv_pct = _pct_change(inv, pri_inv)
    inv_score = (
        -2
        if (inv_pct is not None and rev_pct is not None and inv_pct > rev_pct + 10)
        else (
            1
            if (inv_pct is not None and rev_pct is not None and inv_pct <= rev_pct)
            else 0
        )
    )
    signals["inventory_vs_revenue"] = _signal(
        "inventory_vs_revenue", inv_score, inv, pri_inv, inv_rec.get("evidence", [])
    )

    # Backlog
    bl, bl_rec = _extract_metric(curr, "backlog")
    pri_bl, _ = _extract_metric(prior, "backlog")
    bl_pct = _pct_change(bl, pri_bl)
    bl_score = (
        1
        if (bl_pct is not None and bl_pct > 0)
        else (-1 if (bl_pct is not None and bl_pct < 0) else 0)
    )
    signals["backlog"] = _signal(
        "backlog", bl_score, bl, pri_bl, bl_rec.get("evidence", [])
    )

    # Qualitative signals
    def eval_qual(key: str, default_score: int = 1) -> tuple[int, Any]:
        item = _mapping(qual.get(key))
        pres = item.get("present")
        st = str(item.get("strength") or "").lower()
        ev = item.get("evidence", [])
        if pres or st in {"strong", "moderate", "high", "raised"}:
            sc = 2 if st == "strong" else default_score
            return sc, ev
        return 0, []

    ai_sc, ai_ev = eval_qual("ai_demand", 2)
    signals["ai_demand"] = _signal(
        "ai_demand",
        ai_sc,
        "present" if ai_sc else None,
        None,
        ai_ev,
        basis="report_qualitative",
    )

    dc_sc, dc_ev = eval_qual("data_centre_demand", 1)
    if not dc_sc:
        dc_sc, dc_ev = eval_qual("datacenter_demand", 1)
    signals["data_centre_demand"] = _signal(
        "data_centre_demand",
        dc_sc,
        "present" if dc_sc else None,
        None,
        dc_ev,
        basis="report_qualitative",
    )

    sc_sc, sc_ev = eval_qual("supply_constraints", 1)
    signals["supply_constraints"] = _signal(
        "supply_constraints",
        sc_sc,
        "present" if sc_sc else None,
        None,
        sc_ev,
        basis="report_qualitative",
    )

    pp_sc, pp_ev = eval_qual("pricing_power", 1)
    signals["pricing_power"] = _signal(
        "pricing_power",
        pp_sc,
        "present" if pp_sc else None,
        None,
        pp_ev,
        basis="report_qualitative",
    )

    g_up = _mapping(qual.get("guidance_up"))
    g_dir = _mapping(qual.get("guidance_direction"))
    if g_up.get("present") or str(g_up.get("strength")).lower() == "raised":
        g_sc = 2
        g_ev = g_up.get("evidence", [])
    elif str(g_dir.get("direction")).lower() == "up":
        g_sc = 2
        g_ev = g_dir.get("evidence", [])
    elif str(g_dir.get("direction")).lower() == "down":
        g_sc = -2
        g_ev = g_dir.get("evidence", [])
    else:
        g_sc = 0
        g_ev = []
    signals["guidance_direction"] = _signal(
        "guidance_direction",
        g_sc,
        "raised" if g_sc > 0 else ("cut" if g_sc < 0 else None),
        None,
        g_ev,
        basis="report_qualitative",
    )

    # 5. Score & State
    total_score = max(0, min(10, sum(s["score"] for s in signals.values())))

    mat_covered = {
        "revenue": bool(signals["revenue"]["comparable"]),
        "capex": bool(signals["capex"]["comparable"]),
        "fcf": bool(signals["fcf"]["comparable"]),
        "gross_margin_delta": bool(
            signals["gross_margin_delta"]["comparable"] or gm_curr is not None
        ),
    }
    eligible_for_high = all(mat_covered.values())
    coverage = {
        "eligible_for_high_states": eligible_for_high,
        "covered": [k for k, v in mat_covered.items() if v],
        "uncovered": [k for k, v in mat_covered.items() if not v],
        "material_signals": mat_covered,
    }

    if total_score <= -2:
        stage = "weakening"
    elif total_score < 2:
        stage = "monitor"
    elif total_score < 5:
        stage = "forming"
    elif total_score < 8:
        stage = "confirmed" if eligible_for_high else "forming"
    else:
        stage = "accelerating" if eligible_for_high else "forming"

    # Mature transition check
    p_count = _finite(prior_analysis_count) or 0
    n_count = len(news_items) if isinstance(news_items, (list, tuple)) else 0
    if (
        stage in {"confirmed", "accelerating"}
        and p_count >= 2
        and n_count >= 3
        and (pe and pe >= 25)
    ):
        stage = "mature"

    prev_st = _mapping(previous_state).get("stage") or str(
        previous_state or "unassigned"
    )
    transition = f"{prev_st}->{stage}" if prev_st != stage else f"retained:{stage}"

    # 6. Drivers, Risks, Watch items
    drivers = []
    risks = []
    watch_items = []

    if signals["revenue"]["score"] > 0:
        drivers.append("revenue expansion")
    elif signals["revenue"]["score"] < 0:
        risks.append("revenue contraction")

    if signals["capex"]["score"] > 0:
        drivers.append("capital investment")
    if signals["guidance_direction"]["score"] > 0:
        drivers.append("guidance")

    if signals["inventory_vs_revenue"]["score"] < 0:
        risks.append("inventory relative to revenue")

    for k, v in mat_covered.items():
        if not v:
            watch_items.append(f"{k.replace('_', ' ')}: missing comparable evidence")

    return {
        "metrics": metrics,
        "valuation": valuation,
        "fundamentals": fundamentals,
        "signals": signals,
        "score": total_score,
        "state": {
            "stage": stage,
            "previous_stage": prev_st,
            "score": total_score,
            "transition": transition,
            "coverage": coverage,
            "rule_version": "2",
        },
        "drivers": drivers,
        "risks": risks,
        "watch_items": watch_items,
    }
