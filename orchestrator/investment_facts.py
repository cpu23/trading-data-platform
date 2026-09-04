"""Deterministic financial facts for investment filings.

SEC XBRL facts are selected by accession and fiscal period.  The model never
chooses, changes, or normalizes these values.
"""

from __future__ import annotations

import hashlib
import html
import io
import math
import re
import zipfile
from collections import Counter
from datetime import date
from html.parser import HTMLParser
from typing import Any

from http_client import get_shared_client, make_request

SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MAX_REPORT_PACKAGE_MEMBER_BYTES = 100_000_000
MAX_REPORT_PACKAGE_UNCOMPRESSED_BYTES = 100_000_000


# Ordered aliases: use the most specific standardized concept available.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "diluted_eps": ("EarningsPerShareDiluted",),
    "shares_outstanding": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "CommonStockSharesOutstanding",
    ),
    "inventory": (
        "InventoryNet",
        "InventoryNetOfAllowancesCustomerAdvancesAndProgressBillings",
    ),
    "gross_profit": ("GrossProfit",),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "total_debt": (
        "DebtAndFinanceLeaseObligations",
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebt",
    ),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "equity": (
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ),
}

_DURATION_METRICS = frozenset(
    {
        "revenue",
        "operating_cash_flow",
        "capex",
        "lease_inclusive_investment",
        "net_income",
        "diluted_eps",
        "shares_outstanding",
        "gross_profit",
    }
)
_MONETARY_METRICS = frozenset(
    {
        "revenue",
        "operating_cash_flow",
        "capex",
        "lease_inclusive_investment",
        "net_income",
        "inventory",
        "gross_profit",
        "cash",
        "total_debt",
        "total_assets",
        "total_liabilities",
        "equity",
        "current_assets",
        "current_liabilities",
    }
)

_RELATIONSHIP_METRIC_FAMILIES: dict[str, str] = {
    "revenue": "revenue",
    "net_income": "net_income",
    "diluted_eps": "diluted_eps",
    "operating_cash_flow": "operating_cash_flow",
    "free_cash_flow": "free_cash_flow",
    "capex": "capital_investment",
    "lease_inclusive_investment": "capital_investment",
    "gross_profit": "gross_profit",
    "gross_margin": "gross_margin",
}


def _relationship_tags_for_metric(
    metric: str, *, scope: str = "consolidated"
) -> dict[str, Any] | None:
    """Return issuer-agnostic relationship semantics for a canonical metric."""
    metric_family = _RELATIONSHIP_METRIC_FAMILIES.get(metric)
    if metric_family is None:
        return None
    return {
        "leaf": "standard_metric",
        "metric_family": metric_family,
        "scope": scope,
        "comparison_basis": "none",
        "temporal_basis": (
            "rate_over_period" if metric == "gross_margin" else "period_flow"
        ),
        "cash_basis": (
            "cash_plus_finance_leases"
            if metric == "lease_inclusive_investment"
            else (
                "cash"
                if metric in {"operating_cash_flow", "free_cash_flow", "capex"}
                else "not_applicable"
            )
        ),
        "qualifiers": [],
    }


def _tag_relationship_metric(
    metric: str,
    record: dict[str, Any],
    *,
    scope: str = "consolidated",
    duration_days: int | None = None,
) -> None:
    tags = _relationship_tags_for_metric(metric, scope=scope)
    if tags is not None:
        if isinstance(duration_days, int) and duration_days > 0:
            tags["duration_days"] = duration_days
        record["relationship_tags"] = tags


def _derive_free_cash_flow(
    target: dict[str, dict[str, Any]],
    source: str,
) -> None:
    operating_cash_flow = target.get("operating_cash_flow")
    capital_investment = target.get("capex")
    if not operating_cash_flow or not capital_investment:
        return
    operating_tags = operating_cash_flow.get("relationship_tags", {})
    investment_tags = capital_investment.get("relationship_tags", {})
    if (
        operating_cash_flow["unit"] != capital_investment["unit"]
        or operating_cash_flow["period"] != capital_investment["period"]
        or operating_tags.get("duration_days") != investment_tags.get("duration_days")
    ):
        return
    record = {
        "value": operating_cash_flow["value"] - abs(capital_investment["value"]),
        "unit": operating_cash_flow["unit"],
        "period": operating_cash_flow["period"],
        "evidence": (
            "Deterministic operating cash flow less cash capital investment "
            f"from {operating_cash_flow['concept']} and {capital_investment['concept']}"
        ),
        "source": f"derived_{source}",
        "concept": "derived:operating_cash_flow-capex",
    }
    scope = (
        operating_tags.get("scope")
        if operating_tags.get("scope") == investment_tags.get("scope")
        else "other"
    )
    duration_days = operating_tags.get("duration_days")
    _tag_relationship_metric(
        "free_cash_flow", record, scope=scope, duration_days=duration_days
    )
    target["free_cash_flow"] = record


def sec_cik(document: dict[str, Any]) -> str | None:
    """Return a zero-padded SEC CIK from a filing URL."""
    match = re.search(r"/data/(\d+)(?:/|$)", str(document.get("source_url") or ""))
    if not match:
        return None
    return match.group(1).zfill(10)


def _iso(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _annual_entry(metric: str, entry: dict[str, Any]) -> bool:
    end = _iso(entry.get("end"))
    if end is None:
        return False
    if metric not in _DURATION_METRICS:
        return True
    start = _iso(entry.get("start"))
    return start is not None and 270 <= (end - start).days <= 550


def _unit_rank(metric: str, unit: str) -> tuple[int, str]:
    lowered = unit.casefold()
    if metric == "shares_outstanding":
        return (0 if lowered == "shares" else 5, unit)
    if metric == "diluted_eps":
        return (0 if "/shares" in lowered or "share" in lowered else 5, unit)
    return (
        0
        if unit in {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "CNY", "KRW", "HKD"}
        else 5,
        unit,
    )


def _concept_entries(
    companyfacts: dict[str, Any], metric: str, accession: str
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    if not isinstance(us_gaap, dict):
        return None, None, []
    for concept in CONCEPTS[metric]:
        payload = us_gaap.get(concept)
        units = payload.get("units", {}) if isinstance(payload, dict) else {}
        if not isinstance(units, dict):
            continue
        candidates: list[tuple[str, list[dict[str, Any]]]] = []
        for unit, values in units.items():
            selected = (
                [
                    value
                    for value in values
                    if isinstance(value, dict)
                    and value.get("accn") == accession
                    and not value.get("segment")
                    and isinstance(value.get("val"), (int, float))
                    and not isinstance(value.get("val"), bool)
                    and math.isfinite(float(value["val"]))
                    and _annual_entry(metric, value)
                ]
                if isinstance(values, list)
                else []
            )
            if selected:
                candidates.append((unit, selected))
        if candidates:
            unit, selected = min(
                candidates, key=lambda item: _unit_rank(metric, item[0])
            )
            return concept, unit, selected
    return None, None, []


def _normalize_value(metric: str, value: float, unit: str) -> tuple[float, str]:
    if metric in _MONETARY_METRICS and unit.isalpha() and len(unit) == 3:
        return value / 1_000_000.0, f"{unit}m"
    if metric == "shares_outstanding" and unit.casefold() == "shares":
        return value / 1_000_000.0, "million shares"
    if metric == "diluted_eps" and unit.casefold() in {"usd/shares", "usd/share"}:
        return value, "USD/share"
    return value, unit


def _records(
    companyfacts: dict[str, Any], metric: str, accession: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    concept, unit, entries = _concept_entries(companyfacts, metric, accession)
    if not concept or not unit or not entries:
        return None, None, None
    # Duplicate facts for the same period are common. Prefer the latest filed
    # fact, then retain one value per period end.
    entries.sort(
        key=lambda item: (str(item.get("end") or ""), str(item.get("filed") or "")),
        reverse=True,
    )
    by_end: dict[str, dict[str, Any]] = {}
    for entry in entries:
        by_end.setdefault(str(entry["end"]), entry)
    periods = list(by_end.values())

    def record(entry: dict[str, Any]) -> dict[str, Any]:
        value, normalized_unit = _normalize_value(metric, float(entry["val"]), unit)
        period = str(entry["end"])
        result = {
            "value": value,
            "unit": normalized_unit,
            "period": period,
            "evidence": f"SEC XBRL us-gaap:{concept}; period ending {period}; accession {accession}",
            "source": "sec_xbrl",
            "concept": f"us-gaap:{concept}",
        }
        start = _iso(entry.get("start"))
        end = _iso(entry.get("end"))
        duration_days = (
            (end - start).days if start is not None and end is not None else None
        )
        _tag_relationship_metric(metric, result, duration_days=duration_days)
        return result

    return record(periods[0]), record(periods[1]) if len(periods) > 1 else None, concept


def extract_sec_facts(
    document: dict[str, Any],
    companyfacts: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Select and normalize report facts from an SEC Companyfacts payload."""
    accession = str(document.get("filing_id") or "")
    if not accession:
        return (
            {},
            {},
            {
                "source": "sec_xbrl",
                "status": "unavailable",
                "reason": "missing_accession",
            },
        )
    current: dict[str, dict[str, Any]] = {}
    prior: dict[str, dict[str, Any]] = {}
    concepts: dict[str, str] = {}
    for metric in CONCEPTS:
        latest, previous, concept = _records(companyfacts, metric, accession)
        if latest is not None:
            current[metric] = latest
            concepts[metric] = f"us-gaap:{concept}"
        if previous is not None:
            prior[metric] = previous

    def derive(target: dict[str, dict[str, Any]], *, include_prior: bool) -> None:
        revenue = target.get("revenue")
        gross_profit = target.get("gross_profit")
        if revenue and gross_profit and revenue["value"]:
            target["gross_margin"] = {
                "value": gross_profit["value"] / revenue["value"] * 100.0,
                "unit": "percent",
                "period": revenue["period"],
                "evidence": f"Deterministic gross profit / revenue from {gross_profit['concept']} and {revenue['concept']}",
                "source": "derived_sec_xbrl",
                "concept": "derived:gross_profit/revenue",
            }
            revenue_duration = revenue.get("relationship_tags", {}).get("duration_days")
            gross_duration = gross_profit.get("relationship_tags", {}).get(
                "duration_days"
            )
            _tag_relationship_metric(
                "gross_margin",
                target["gross_margin"],
                duration_days=(
                    revenue_duration if revenue_duration == gross_duration else None
                ),
            )
        debt = target.get("total_debt")
        cash = target.get("cash")
        if debt and cash and debt["unit"] == cash["unit"]:
            target["net_debt"] = {
                "value": debt["value"] - cash["value"],
                "unit": debt["unit"],
                "period": max(str(debt["period"]), str(cash["period"])),
                "evidence": f"Deterministic total debt less cash from {debt['concept']} and {cash['concept']}",
                "source": "derived_sec_xbrl",
                "concept": "derived:debt-cash",
            }
        _derive_free_cash_flow(target, "sec_xbrl")

    derive(current, include_prior=False)
    derive(prior, include_prior=True)
    status = "success" if current else "unavailable"
    return (
        current,
        prior,
        {
            "source": "sec_xbrl",
            "status": status,
            "deterministic_metric_count": len(current),
            "concepts": concepts,
            "accession": accession,
        },
    )


_REPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": (
        "revenue",
        "revenues",
        "turnover",
        "salesrevenue",
        "revenuefromcontractwithcustomerexcludingassessedtax",
    ),
    "operating_cash_flow": (
        "netcashprovidedbyusedinoperatingactivities",
        "cashflowsfromusedinoperatingactivities",
        "operatingcashflow",
        "netcashfromoperatingactivities",
        "netcashinflowoutflowfromoperatingactivities",
    ),
    "capex": (
        "paymentstoacquirepropertyplantandequipment",
        "paymentsforadditions topropertyplantandequipment".replace(" ", ""),
        "capitalexpenditure",
        "purchaseofpropertyplantandequipment",
    ),
    "lease_inclusive_investment": (
        "capitalexpenditureincludingfinanceleaseadditions",
        "capitalexpendituresincludingfinanceleaseadditions",
        "purchasesofpropertyplantandequipmentandfinanceleaseadditions",
    ),
    "net_income": (
        "netincomeloss",
        "profitloss",
        "profitfortheyear",
        "profitlossattributabletoownersofparent",
        "profitaftertax",
    ),
    "diluted_eps": (
        "earningspersharediluted",
        "dilutedeps",
        "dilutedearningspershare",
    ),
    "shares_outstanding": (
        "weightedaveragenumberofdilutedsharesoutstanding",
        "weightedaveragenumberofshares",
        "weightedaveragenumberofordinarysharesdiluted",
        "sharesoutstanding",
    ),
    "inventory": ("inventorynet", "inventory", "inventories"),
    "gross_profit": ("grossprofit", "grossprofitloss"),
    "cash": (
        "cashandcashequivalentsatcarryingvalue",
        "cashandcashequivalents",
        "cash",
        "cashcashEquivalentsrestrictedcashandre",  # uncommon UK label
    ),
    "total_debt": (
        "debtandfinanceleaseobligations",
        "longtermdebt",
        "borrowings",
        "financialliabilities",
        "totaldebt",
    ),
    "total_assets": ("assets", "totalassets"),
    "total_liabilities": ("liabilities", "totalliabilities"),
    "current_assets": ("assetscurrent", "currentassets"),
    "current_liabilities": ("liabilitiescurrent", "currentliabilities"),
    "equity": (
        "stockholdersequity",
        "equity",
        "totalshareholdersfunds",
        "equityattributabletoownersofparent",
    ),
}

_STANDARD_REPORT_NAMESPACES = frozenset({"ifrs-full", "us-gaap", "uk-gaap"})

_TEXT_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "operating_cash_flow",
        (
            "net cash from operating activities",
            "net cash generated from operating activities",
            "net cash inflow from operating activities",
            "net cash inflows from operating activities",
            "operating cash flow",
        ),
    ),
    ("current_liabilities", ("current liabilities",)),
    ("current_assets", ("current assets",)),
    ("total_liabilities", ("total liabilities",)),
    ("total_assets", ("total assets",)),
    ("gross_profit", ("gross profit", "gross profit/(loss)")),
    (
        "net_income",
        (
            "profit after tax for the year",
            "net income",
            "profit for the year",
            "profit for the financial year",
            "profit after tax",
            "profit attributable",
        ),
    ),
    (
        "revenue",
        (
            "revenue from contracts with customers",
            "total revenue",
            "insurance revenue",
            "revenue",
            "revenues",
            "turnover",
            "sales",
        ),
    ),
    (
        "lease_inclusive_investment",
        (
            "capital expenditure including finance lease additions",
            "capital expenditures including finance lease additions",
            "purchases of property, plant and equipment and finance lease additions",
        ),
    ),
    (
        "capex",
        (
            "capital expenditure",
            "capital expenditures",
            "purchase of property, plant and equipment",
            "purchase of plant and equipment",
            "expenditure on property, plant and equipment",
        ),
    ),
    ("inventory", ("inventory", "inventories")),
    (
        "cash",
        (
            "cash and cash equivalents",
            "cash and cesh equivalents",
            "cash equivalents",
            "cash",
        ),
    ),
    ("total_debt", ("total debt", "borrowings", "debt")),
    (
        "equity",
        ("total equity", "shareholders' equity", "shareholders funds", "equity"),
    ),
)

_CURRENCY_CODES = {
    "£": "GBP",
    "$": "USD",
    "€": "EUR",
    "¥": "JPY",
    "GBP": "GBP",
    "USD": "USD",
    "EUR": "EUR",
    "JPY": "JPY",
    "CAD": "CAD",
    "AUD": "AUD",
    "CHF": "CHF",
}
_NUM_RE = re.compile(
    r"(?<![A-Za-z])(?:[£$€¥]\s*)?(?:\(?\s*[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)\s*\)?)(?![A-Za-z])"
)
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_STATEMENT_ANCHOR_RE = re.compile(
    r"^\s*(?:consolidated\s+)?(?:income\s+statement|statement\s+of\s+(?:income|"
    r"profit\s+or\s+loss|comprehensive\s+income|financial\s+position|cash\s+flows)|"
    r"balance\s+sheet|cash\s+flow(?:s)?\s+statement|profit\s+and\s+loss)"
    r"\s*[.:,]?\s*$",
    re.I,
)


def _compact_concept(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().split(":")[-1])


def _report_metric(concept: str) -> tuple[str, int] | None:
    compact = _compact_concept(concept)
    for metric, aliases in _REPORT_ALIASES.items():
        for rank, alias in enumerate(aliases):
            if compact == _compact_concept(alias):
                return metric, rank
    return None


def _well_formed_thousands(value: str) -> bool:
    """Fail closed on truncated or foreign-locale comma groups.

    Every thousands group after the first must be exactly three digits; a
    short trailing group ('1,30' for '1,30x') means OCR dropped a digit or a
    decimal comma slipped in, and the true magnitude cannot be recovered.
    """
    core = value.lstrip("+-")
    if "," not in core:
        return True
    groups = core.split(".", 1)[0].split(",")
    return len(groups[0]) <= 3 and all(len(group) == 3 for group in groups[1:])


def _parse_number(value: str) -> float | None:
    value = html.unescape(value).replace("\xa0", " ").strip()
    if not value or value in {"-", "—", "–", "−", "n/a", "N/A"}:
        return None
    negative = value.startswith("(") and value.endswith(")")
    value = value.strip("() ")
    value = re.sub(r"^[£$€¥]\s*", "", value)
    value = value.replace(" ", "").replace("−", "-")
    if not _well_formed_thousands(value):
        return None
    value = value.replace(",", "")
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return -number if negative else number


def _number_core(value: str) -> str:
    """Digits and separators only, with sign, currency, and parentheses removed."""
    core = html.unescape(value).replace("\xa0", " ").strip()
    core = core.strip("() ")
    core = re.sub(r"^[£$€¥]\s*", "", core)
    return core.replace(" ", "").replace("−", "-").lstrip("+-")


def _pair_plausible(current: float, prior: float) -> bool:
    """True when aligned comparative values share a plausible magnitude."""
    current_abs = abs(current)
    prior_abs = abs(prior)
    if not current_abs or not prior_abs:
        return True
    ratio = current_abs / prior_abs
    return (1 / 3) <= ratio <= 3.0


def _separator_loss_suspect(value: str) -> bool:
    """True when a value reads like a thousands comma OCR'd as a dot.

    A dot without a comma and at least three fractional digits is the OCR
    signature of a lost separator ('3.870' for 3,870, '5.66390' for
    5,663.9).  A leading-zero integer part ('0.025') marks a genuine
    sub-unit decimal and is never reinterpreted.
    """
    core = _number_core(value)
    if "," in core or "." not in core:
        return False
    integer_part, _, fraction = core.partition(".")
    return len(fraction) >= 3 and not integer_part.startswith("0")


def _decimal_places(value: str) -> int:
    """Fractional digit count of a displayed number."""
    core = _number_core(value)
    if "." not in core:
        return 0
    return len(core.split(".", 1)[1])


def _recover_value(
    value_raw: str, naive: float, peer_raw: str, peer: float
) -> float | None:
    """Thousands-scale reading of a separator-lost column value, or None.

    Every digit placement is scored against the aligned peer value and the
    peer column's decimal formatting; exactly one plausible placement is
    adopted, otherwise the value fails closed.  OCR sometimes pads a value
    with a stray trailing zero ('5.66390' for 5,663.9) or drops both
    separators outright ('13160' for 1,316.0); padding is dropped only while
    the digit string still exceeds the aligned peer column's width.
    """
    core = _number_core(value_raw)
    digits = core.replace(",", "").replace(".", "")
    if not digits:
        return None
    peer_digits = _number_core(peer_raw).replace(",", "").replace(".", "")
    if peer_digits and len(digits) > len(peer_digits):
        stripped = digits.rstrip("0")
        if len(stripped) <= len(peer_digits):
            digits = stripped
    magnitude = abs(int(digits))
    peer_places = _decimal_places(peer_raw)
    candidates: list[float] = []
    for places in range(len(digits) + 1):
        candidate = magnitude / (10.0**places)
        if places != peer_places:
            continue
        if _pair_plausible(candidate, peer):
            candidates.append(candidate)
    if len(candidates) != 1:
        return None
    return -candidates[0] if naive < 0 else candidates[0]


def _stripped_integer_suspect(value: str, peer: str) -> bool:
    """True when a separator-free integer may be a comma/dot-stripped value.

    A pure integer aligned with a thousands-grouped decimal column ('13160'
    beside '2,071.3') can be OCR's loss of both separators ('1,316.0').
    Recovery needs that column evidence, because genuine small integers never
    align with a grouped decimal peer.
    """
    core = _number_core(value)
    peer_core = _number_core(peer)
    return "." not in core and "," not in core and "." in peer_core and "," in peer_core


def _parse_number_pair(current_raw: str, prior_raw: str) -> tuple[float, float] | None:
    """Parse an aligned current/prior row, reconciling OCR separator loss.

    A single column whose thousands comma was misread as a decimal point
    ('3.870' for 3,870, '5.66390' for 5,663.9) is recovered only when the
    aligned peer value admits exactly one thousands-scale reading; a pure
    integer that lost both separators ('13160' for 1,316.0 beside '2,071.3')
    is recovered the same way.  Truncated figures ('1,30') and ambiguous
    pairs fail closed instead of emitting an implausible magnitude; valid
    decimals and negatives pass through.
    """
    current = _parse_number(current_raw)
    prior = _parse_number(prior_raw)
    if current is None or prior is None:
        return None
    if _pair_plausible(current, prior):
        return current, prior
    current_suspect = _separator_loss_suspect(current_raw)
    prior_suspect = _separator_loss_suspect(prior_raw)
    if current_suspect != prior_suspect:
        if current_suspect:
            recovered = _recover_value(current_raw, current, prior_raw, prior)
            if recovered is None:
                return None
            return recovered, prior
        recovered = _recover_value(prior_raw, prior, current_raw, current)
        if recovered is None:
            return None
        return current, recovered
    if not current_suspect:
        # Neither side shows a dot-no-comma separator loss, but an implausible
        # pair whose single pure-integer side aligns with a thousands-grouped
        # decimal column may have lost its comma and decimal point entirely.
        current_integer = _stripped_integer_suspect(current_raw, prior_raw)
        prior_integer = _stripped_integer_suspect(prior_raw, current_raw)
        if current_integer != prior_integer:
            if current_integer:
                recovered = _recover_value(current_raw, current, prior_raw, prior)
                if recovered is None:
                    return None
                return recovered, prior
            recovered = _recover_value(prior_raw, prior, current_raw, current)
            if recovered is None:
                return None
            return current, recovered
    # Both columns lost their separators (indistinguishable from genuine
    # decimals) or neither is recoverable; keep the literal parse and let the
    # existing per-metric validation decide.
    return current, prior


def _unit_currency(unit: str) -> str | None:
    text = unit.strip().upper().replace("ISO4217:", "")
    if text in _CURRENCY_CODES:
        return _CURRENCY_CODES[text]
    for token, code in _CURRENCY_CODES.items():
        if len(token) > 1 and token in text:
            return code
    return None


def _derive_report_metrics(
    current: dict[str, dict[str, Any]],
    prior: dict[str, dict[str, Any]],
    source: str,
) -> None:
    for target in (current, prior):
        revenue = target.get("revenue")
        gross = target.get("gross_profit")
        if (
            revenue
            and gross
            and revenue["unit"] == gross["unit"]
            and revenue["period"] == gross["period"]
            and revenue["value"]
        ):
            target["gross_margin"] = {
                "value": gross["value"] / revenue["value"] * 100.0,
                "unit": "percent",
                "period": revenue["period"],
                "evidence": f"Deterministic gross profit / revenue from {gross['concept']} and {revenue['concept']}",
                "source": f"derived_{source}",
                "concept": "derived:gross_profit/revenue",
            }
            revenue_scope = revenue.get("relationship_tags", {}).get("scope")
            gross_scope = gross.get("relationship_tags", {}).get("scope")
            revenue_duration = revenue.get("relationship_tags", {}).get("duration_days")
            gross_duration = gross.get("relationship_tags", {}).get("duration_days")
            _tag_relationship_metric(
                "gross_margin",
                target["gross_margin"],
                scope=revenue_scope if revenue_scope == gross_scope else "other",
                duration_days=(
                    revenue_duration if revenue_duration == gross_duration else None
                ),
            )
        debt = target.get("total_debt")
        cash = target.get("cash")
        if (
            debt
            and cash
            and debt["unit"] == cash["unit"]
            and debt["period"] == cash["period"]
        ):
            target["net_debt"] = {
                "value": debt["value"] - cash["value"],
                "unit": debt["unit"],
                "period": debt["period"],
                "evidence": f"Deterministic total debt less cash from {debt['concept']} and {cash['concept']}",
                "source": f"derived_{source}",
                "concept": "derived:debt-cash",
            }
        _derive_free_cash_flow(target, source)


class _InlineFactParser(HTMLParser):
    """Collect inline numeric facts while retaining their exact source span."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.facts: list[dict[str, Any]] = []
        self._active: list[dict[str, Any]] = []

    def _append(self, text: str) -> None:
        for fact in self._active:
            fact["text_parts"].append(text)
            fact["evidence_parts"].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or f"<{tag}>"
        self._append(raw)
        lowered = tag.casefold().split(":")[-1]
        if lowered not in {"nonfraction"}:
            return
        values = {str(k).casefold(): v for k, v in attrs}
        fact = {
            "name": values.get("name") or "",
            "contextref": values.get("contextref")
            or values.get("contextref".casefold()),
            "unitref": values.get("unitref") or "",
            "scale": values.get("scale") or "0",
            "sign": values.get("sign") or "",
            "nil": values.get("nil") or "",
            "tag": lowered,
            "text_parts": [],
            "evidence_parts": [raw],
        }
        self._active.append(fact)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self._append(data)

    def handle_entityref(self, name: str) -> None:
        self._append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        raw = f"</{tag}>"
        for fact in self._active:
            fact["evidence_parts"].append(raw)
        lowered = tag.casefold().split(":")[-1]
        if lowered == "nonfraction" and self._active:
            fact = self._active.pop()
            fact["text"] = "".join(fact.pop("text_parts"))
            fact["evidence"] = "".join(fact.pop("evidence_parts"))
            self.facts.append(fact)


def _inline_contexts(markup: str) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for match in re.finditer(
        r"<(?:[\w.-]+:)?context\b[^>]*>(.*?)</(?:[\w.-]+:)?context\s*>",
        markup,
        re.I | re.S,
    ):
        body = match.group(1)
        attrs = dict(
            re.findall(
                r"\b([\w.-]+)\s*=\s*['\"]([^'\"]*)['\"]",
                match.group(0)[: match.group(0).find(">") + 1],
            )
        )
        context_id = attrs.get("id") or attrs.get("ID")
        if not context_id or re.search(r"(?:explicitMember|typedMember)\b", body, re.I):
            continue
        instant = re.search(r"<(?:[\w.-]+:)?instant\s*>([^<]+)", body, re.I)
        start = re.search(r"<(?:[\w.-]+:)?startDate\s*>([^<]+)", body, re.I)
        end = re.search(r"<(?:[\w.-]+:)?endDate\s*>([^<]+)", body, re.I)
        if instant:
            end_value, start_value = instant.group(1).strip(), None
        elif end:
            end_value, start_value = (
                end.group(1).strip(),
                start.group(1).strip() if start else None,
            )
        else:
            continue
        end_date = _iso(end_value)
        start_date = _iso(start_value) if start_value else None
        if end_date is None:
            continue
        annual = start_date is None or 270 <= (end_date - start_date).days <= 550
        contexts[context_id] = {
            "end": end_value,
            "start": start_value,
            "annual": annual,
            "instant": start_date is None,
        }
    return contexts


def _inline_units(markup: str) -> dict[str, str]:
    units: dict[str, str] = {}
    for match in re.finditer(
        r"<(?:[\w.-]+:)?unit\b([^>]*)>(.*?)</(?:[\w.-]+:)?unit\s*>", markup, re.I | re.S
    ):
        attrs = dict(
            re.findall(r"\b([\w.-]+)\s*=\s*['\"]([^'\"]*)['\"]", match.group(1))
        )
        if not attrs.get("id"):
            continue
        measures = re.findall(r"<(?:[\w.-]+:)?measure\s*>([^<]+)", match.group(2), re.I)
        if len(measures) == 1:
            units[attrs["id"]] = measures[0].strip()
        elif len(measures) >= 2:
            units[attrs["id"]] = f"{measures[0].strip()}/{measures[1].strip()}"
    return units


def _inline_value(fact: dict[str, Any]) -> float | None:
    if str(fact.get("nil", "")).casefold() == "true":
        return None
    value = _parse_number(fact.get("text", ""))
    if value is None:
        return None
    try:
        value *= 10.0 ** int(str(fact.get("scale", "0")).strip() or "0")
    except (TypeError, ValueError, OverflowError):
        return None
    if str(fact.get("sign", "")).strip() == "-":
        value = -value
    return value if math.isfinite(value) else None


def extract_ixbrl_facts(
    raw_content: bytes | str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Extract consolidated annual facts from an inline-XBRL report package."""
    raw = (
        raw_content.encode("utf-8", "replace")
        if isinstance(raw_content, str)
        else bytes(raw_content)
    )
    if not raw:
        return (
            {},
            {},
            {
                "source": "uk_ixbrl",
                "status": "unavailable",
                "reason": "empty_content",
                "deterministic_metric_count": 0,
            },
        )
    files: list[tuple[str, bytes]] = []
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            candidates = sorted(
                (
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and info.file_size <= MAX_REPORT_PACKAGE_MEMBER_BYTES
                    and info.filename.casefold().endswith(
                        (".html", ".htm", ".xhtml", ".xml")
                    )
                ),
                key=lambda info: (
                    not info.filename.casefold().endswith((".html", ".htm", ".xhtml")),
                    -info.file_size,
                ),
            )
            for info in candidates:
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_REPORT_PACKAGE_UNCOMPRESSED_BYTES:
                    break
                files.append((info.filename, archive.read(info)))
    except (zipfile.BadZipFile, OSError):
        files = []
    if not files:
        return (
            {},
            {},
            {
                "source": "uk_ixbrl",
                "status": "unavailable",
                "reason": "not_report_package",
                "deterministic_metric_count": 0,
            },
        )
    markup = "\n".join(data.decode("utf-8", "replace") for _, data in files)
    contexts = _inline_contexts(markup)
    units = _inline_units(markup)
    parser = _InlineFactParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return (
            {},
            {},
            {
                "source": "uk_ixbrl",
                "status": "unavailable",
                "reason": "malformed_markup",
                "deterministic_metric_count": 0,
            },
        )
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in parser.facts:
        concept = str(fact.get("name") or "")
        mapped = _report_metric(concept)
        context = contexts.get(str(fact.get("contextref") or ""))
        unit = units.get(str(fact.get("unitref") or ""), str(fact.get("unitref") or ""))
        value = _inline_value(fact)
        if not mapped or not context or value is None or not context["annual"]:
            continue
        metric, rank = mapped
        namespace = concept.casefold().split(":", 1)[0] if ":" in concept else ""
        if namespace not in _STANDARD_REPORT_NAMESPACES:
            rank += 100
        currency = _unit_currency(unit)
        lowered_unit = unit.casefold()
        if metric in _MONETARY_METRICS:
            if currency is None or "/" in unit:
                continue
            value /= 1_000_000.0
            normalized_unit = f"{currency}m"
        elif metric == "shares_outstanding":
            if "share" not in lowered_unit:
                continue
            value /= 1_000_000.0
            normalized_unit = "million shares"
        elif metric == "diluted_eps":
            if currency is None or (
                "share" not in lowered_unit and "/" not in lowered_unit
            ):
                continue
            normalized_unit = f"{currency}/share"
        else:
            continue
        if metric in _DURATION_METRICS and context["instant"]:
            continue
        if metric not in _DURATION_METRICS and not context["instant"]:
            continue
        key = (metric, str(context["end"]))
        candidate = {
            "value": value,
            "unit": normalized_unit,
            "period": str(context["end"]),
            "evidence": fact["evidence"],
            "source": "uk_ixbrl",
            "concept": concept,
            "_rank": rank,
        }
        start = _iso(context.get("start"))
        end = _iso(context.get("end"))
        duration_days = (
            (end - start).days if start is not None and end is not None else None
        )
        _tag_relationship_metric(metric, candidate, duration_days=duration_days)
        candidates.setdefault(key, []).append(candidate)
    selected: dict[str, list[dict[str, Any]]] = {}
    for (metric, _period), values in candidates.items():
        best_rank = min(item["_rank"] for item in values)
        best = [item for item in values if item["_rank"] == best_rank]
        signatures = {(item["value"], item["unit"]) for item in best}
        if len(signatures) != 1:
            continue
        selected.setdefault(metric, []).append(best[0])
    periods = sorted(
        {item["period"] for values in selected.values() for item in values},
        reverse=True,
    )[:2]
    current: dict[str, dict[str, Any]] = {}
    prior: dict[str, dict[str, Any]] = {}
    for metric, values in selected.items():
        by_period = {item["period"]: item for item in values}
        for index, target in enumerate(periods):
            item = by_period.get(target)
            if item is None:
                continue
            normalized = {key: value for key, value in item.items() if key != "_rank"}
            (current if index == 0 else prior)[metric] = normalized
    _derive_report_metrics(current, prior, "uk_ixbrl")
    count = len(current)
    return (
        current,
        prior,
        {
            "source": "uk_ixbrl",
            "status": "success" if count else "unavailable",
            "deterministic_metric_count": count,
            "extracted_fact_count": sum(len(v) for v in selected.values()),
            "fact_count": sum(len(v) for v in selected.values()),
            "periods": periods,
            "contexts": len(contexts),
            "files": len(files),
            "concepts": {metric: item["concept"] for metric, item in current.items()},
        },
    )


_MAX_EXTERNAL_EFFECT_TEXT_CHARS = 100_000
_MAX_EXTERNAL_EFFECT_SENTENCES = 64
_MAX_EXTERNAL_EFFECT_SENTENCE_CHARS = 1_200
_MAX_EXTERNAL_EFFECT_CLAUSES = 4
_MAX_EXTERNAL_EFFECT_FACTS = 8

_EXTERNAL_RECIPIENTS: tuple[tuple[str, str], ...] = (
    ("diluted earnings per share", "diluted_eps"),
    ("diluted eps", "diluted_eps"),
    ("earnings per share", "diluted_eps"),
    ("operating cash flow", "operating_cash_flow"),
    ("cash flow from operations", "operating_cash_flow"),
    ("free cash flow", "free_cash_flow"),
    ("operating margin", "operating_margin"),
    ("gross margin", "gross_margin"),
    ("operating income", "operating_income"),
    ("operating profit", "operating_income"),
    ("net income", "net_income"),
    ("net profit", "net_income"),
    ("gross profit", "gross_profit"),
    ("net revenue", "revenue"),
    ("revenue", "revenue"),
    ("sales", "revenue"),
)
_EXTERNAL_RECIPIENT_PATTERN = (
    r"(?:(?P<recipient_scope>consolidated|segment|product)\s+)?(?P<recipient>"
    + "|".join(re.escape(label) for label, _ in _EXTERNAL_RECIPIENTS)
    + r")(?:'s)?(?:\s+(?:year[- ]over[- ]year|yoy|reported|organic|"
    r"constant[- ]currency))?(?:\s+(?:growth|margin|change))?\b"
)
_EXTERNAL_AMOUNT_PATTERN = (
    r"(?P<amount>(?:(?:GBP|USD|EUR|CAD|AUD|CHF|JPY)\s*|[£$€¥]\s*)?"
    r"\(?[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)\)?)"
)
_EXTERNAL_BASIS_PATTERN = (
    r"(?P<basis>percentage points?|percent points?|points?|per share|"
    r"cents? per share|thousand|million|billion|k|m|bn)"
)
_EXTERNAL_EFFECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "contribution",
        re.compile(
            rf"\bcontribut(?:ed|ion of)\s+{_EXTERNAL_AMOUNT_PATTERN}\s*"
            rf"{_EXTERNAL_BASIS_PATTERN}\s+(?:to|toward)\s+"
            rf"{_EXTERNAL_RECIPIENT_PATTERN}",
            re.I,
        ),
    ),
    (
        "contribution",
        re.compile(
            rf"{_EXTERNAL_AMOUNT_PATTERN}\s*{_EXTERNAL_BASIS_PATTERN}\s+"
            rf"contribution\s+to\s+{_EXTERNAL_RECIPIENT_PATTERN}",
            re.I,
        ),
    ),
    (
        "drag",
        re.compile(
            rf"{_EXTERNAL_AMOUNT_PATTERN}\s*{_EXTERNAL_BASIS_PATTERN}\s+"
            rf"(?:of\s+)?(?:drag|headwind)\s+on\s+"
            rf"{_EXTERNAL_RECIPIENT_PATTERN}",
            re.I,
        ),
    ),
    (
        "drag",
        re.compile(
            rf"\b(?:drag|headwind)\s+of\s+{_EXTERNAL_AMOUNT_PATTERN}\s*"
            rf"{_EXTERNAL_BASIS_PATTERN}\s+on\s+"
            rf"{_EXTERNAL_RECIPIENT_PATTERN}",
            re.I,
        ),
    ),
    (
        "contribution",
        re.compile(
            rf"\bpositive impact of\s+{_EXTERNAL_AMOUNT_PATTERN}\s*"
            rf"{_EXTERNAL_BASIS_PATTERN}\s+on\s+{_EXTERNAL_RECIPIENT_PATTERN}",
            re.I,
        ),
    ),
    (
        "drag",
        re.compile(
            rf"\bnegative impact of\s+{_EXTERNAL_AMOUNT_PATTERN}\s*"
            rf"{_EXTERNAL_BASIS_PATTERN}\s+on\s+{_EXTERNAL_RECIPIENT_PATTERN}",
            re.I,
        ),
    ),
    (
        "reclassification",
        re.compile(
            rf"{_EXTERNAL_AMOUNT_PATTERN}\s*{_EXTERNAL_BASIS_PATTERN}\s+"
            rf"(?:was|were)\s+reclassified\s+from\s+"
            rf"{_EXTERNAL_RECIPIENT_PATTERN}\s+to\s+"
            rf"(?:(?P<recipient_to_scope>consolidated|segment|product)\s+)?"
            rf"(?P<recipient_to>"
            + "|".join(re.escape(label) for label, _ in _EXTERNAL_RECIPIENTS)
            + r")\b",
            re.I,
        ),
    ),
)


def _external_recipient_metric(label: str) -> str | None:
    normalized = re.sub(r"[^a-z]+", " ", label.casefold()).strip()
    for recipient, metric in _EXTERNAL_RECIPIENTS:
        if normalized == recipient:
            return metric
    return None


def _external_category(clause: str, effect_kind: str) -> str:
    categories = (
        (
            r"\b(?:due to|from|related to)\s+(?:a\s+)?business combination\b",
            "business_combination",
        ),
        (
            r"\b(?:due to|from|related to)\s+(?:foreign exchange|currency translation)\b",
            "foreign_exchange",
        ),
        (
            r"\b(?:due to|from|related to)\s+(?:a\s+)?change in accounting estimate\b",
            "accounting_estimate",
        ),
        (
            r"\b(?:due to|from|related to)\s+(?:a\s+)?(?:disposition|divestiture)\b",
            "disposition",
        ),
        (
            r"\b(?:due to|from|related to)\s+restructuring\b",
            "restructuring",
        ),
    )
    for pattern, category in categories:
        if re.search(pattern, clause, re.I):
            return category
    return "other"


def _external_qualifiers(sentence: str, effect_kind: str) -> list[str]:
    qualifiers: list[str] = []
    checks = (
        (r"\b(?:approximately|approx\.?|about)\b", "approximate"),
        (r"\bnet (?:impact|effect|contribution|drag)\b", "net"),
        (r"\bgross (?:impact|effect|contribution|drag)\b", "gross"),
        (r"\breported\b", "reported"),
        (r"\bpurchase accounting\b", "includes_purchase_accounting"),
        (r"\bintegration costs?\b", "includes_integration_costs"),
        (r"\btransaction costs?\b", "includes_transaction_costs"),
    )
    for pattern, qualifier in checks:
        if re.search(pattern, sentence, re.I):
            qualifiers.append(qualifier)
    if effect_kind == "reclassification":
        qualifiers.append("includes_reclassification")
    return qualifiers[:8]


_EXTERNAL_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
_EXTERNAL_ISO_DATE_RE = re.compile(
    r"\b(19\d{2}|20\d{2})-(0?[1-9]|1[0-2])-([0-2]?\d|3[01])\b"
)
_EXTERNAL_MDY_DATE_RE = re.compile(
    rf"\b({_EXTERNAL_MONTH_PATTERN})\s+([0-2]?\d|3[01])(?:st|nd|rd|th)?"
    r",?\s+(19\d{2}|20\d{2})\b",
    re.I,
)
_EXTERNAL_DMY_DATE_RE = re.compile(
    rf"\b([0-2]?\d|3[01])(?:st|nd|rd|th)?\s+({_EXTERNAL_MONTH_PATTERN})"
    r"\s+(19\d{2}|20\d{2})\b",
    re.I,
)
_EXTERNAL_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def _external_period(clause: str) -> str | None:
    iso_match = _EXTERNAL_ISO_DATE_RE.search(clause)
    if iso_match:
        year, month, day = (int(value) for value in iso_match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    mdy_match = _EXTERNAL_MDY_DATE_RE.search(clause)
    if mdy_match:
        month_label, day_label, year_label = mdy_match.groups()
        try:
            return date(
                int(year_label),
                _EXTERNAL_MONTH_NUMBERS[month_label[:3].casefold()],
                int(day_label),
            ).isoformat()
        except (KeyError, ValueError):
            return None
    dmy_match = _EXTERNAL_DMY_DATE_RE.search(clause)
    if dmy_match:
        day_label, month_label, year_label = dmy_match.groups()
        try:
            return date(
                int(year_label),
                _EXTERNAL_MONTH_NUMBERS[month_label[:3].casefold()],
                int(day_label),
            ).isoformat()
        except (KeyError, ValueError):
            return None
    explicit = _YEAR_RE.search(clause)
    return explicit.group(1) if explicit else None


def _external_duration_days(
    clause: str,
    current: dict[str, dict[str, Any]],
    metric: str | None,
    period: str | None,
) -> int | None:
    if period is None or not re.search(r"\byear ended\b", clause, re.I):
        return None
    recipient = current.get(metric or "")
    if not isinstance(recipient, dict) or str(recipient.get("period") or "") != period:
        return None
    tags = recipient.get("relationship_tags")
    duration = tags.get("duration_days") if isinstance(tags, dict) else None
    if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
        return duration
    return None


def _external_scope(
    clause: str,
    current: dict[str, dict[str, Any]],
    metric: str | None,
    *,
    recipient_scope: str | None = None,
    use_clause_scope: bool = True,
) -> str | None:
    if recipient_scope is not None:
        return recipient_scope.casefold()
    if not use_clause_scope:
        clause = ""
    if re.search(r"\bconsolidated\b", clause, re.I):
        return "consolidated"
    if re.search(r"\bsegment\b", clause, re.I):
        return "segment"
    if re.search(r"\bproduct\b", clause, re.I):
        return "product"
    fact = current.get(metric or "")
    tags = fact.get("relationship_tags") if isinstance(fact, dict) else None
    scope = tags.get("scope") if isinstance(tags, dict) else None
    return scope if scope in {"consolidated", "segment", "product"} else None


def _external_comparison_basis(clause: str) -> str:
    if re.search(r"\bconstant[- ]currency\b", clause, re.I):
        return "year_over_year_constant_currency"
    if re.search(r"\b(?:year[- ]over[- ]year|yoy|reported)\b", clause, re.I):
        return "year_over_year_gaap"
    if re.search(r"\bsequential(?:ly)?\b", clause, re.I):
        return "sequential"
    return "none"


def _external_effect_basis(match: re.Match[str]) -> str:
    basis = match.group("basis").casefold()
    if "point" in basis:
        return "percentage_points"
    if "per share" in basis:
        return "per_share"
    return "monetary"


def _external_temporal_basis(match: re.Match[str]) -> str:
    return (
        "rate_over_period"
        if _external_effect_basis(match) == "percentage_points"
        else "period_flow"
    )


def _external_value_and_unit(
    match: re.Match[str], effect_kind: str
) -> tuple[float, str] | None:
    raw = match.group("amount")
    numeric_raw = re.sub(r"^(?:GBP|USD|EUR|CAD|AUD|CHF|JPY)\s*", "", raw, flags=re.I)
    value = _parse_number(numeric_raw)
    if value is None:
        return None
    basis = match.group("basis").casefold()
    if "point" in basis:
        unit = "percentage_points"
    elif "per share" in basis:
        if "cent" in basis:
            value /= 100.0
        currency = next(
            (code for token, code in _CURRENCY_CODES.items() if token in raw.upper()),
            None,
        )
        unit = f"{currency}/share" if currency else "per_share"
    else:
        value *= {
            "thousand": 0.001,
            "k": 0.001,
            "million": 1.0,
            "m": 1.0,
            "billion": 1_000.0,
            "bn": 1_000.0,
        }[basis]
        currency = next(
            (code for token, code in _CURRENCY_CODES.items() if token in raw.upper()),
            None,
        )
        unit = f"{currency}m" if currency else "report_millions"
    if effect_kind == "drag":
        value = -abs(value)
    return value, unit


def _external_effect_leaf(
    *,
    match: re.Match[str],
    effect_kind: str,
    sentence: str,
    clause: str,
    group_id: str,
    current: dict[str, dict[str, Any]],
    recipient_label: str,
    sign: int = 1,
    recipient_scope: str | None = None,
    use_clause_scope: bool = True,
) -> dict[str, Any] | None:
    parsed = _external_value_and_unit(match, effect_kind)
    if parsed is None:
        return None
    value, unit = parsed
    if effect_kind == "reclassification":
        value = abs(value)
    metric = _external_recipient_metric(recipient_label)
    recipient_path = f"current.{metric}" if metric and metric in current else None
    period = _external_period(clause)
    scope = _external_scope(
        clause,
        current,
        metric,
        recipient_scope=recipient_scope,
        use_clause_scope=use_clause_scope,
    )
    reasons = []
    if effect_kind == "contribution" and value < 0:
        reasons.append("unsupported_derivation")
    if recipient_path is None:
        reasons.append("unresolved_recipient")
    if period is None:
        reasons.append("period_mismatch")
    if scope is None:
        reasons.append("scope_mismatch")
    basis = _external_effect_basis(match)
    recipient_text = match.group(0).casefold()
    if (
        basis == "percentage_points"
        and metric not in {"gross_margin", "operating_margin"}
        and not re.search(r"\b(?:growth|change)\b", recipient_text)
    ):
        reasons.append("unit_mismatch")
    if basis == "per_share" and metric != "diluted_eps":
        reasons.append("unit_mismatch")
    if basis == "monetary" and unit == "report_millions":
        reasons.append("currency_mismatch")
    if re.search(r"\borganic\b", recipient_text):
        reasons.append("unsupported_derivation")
    tags = {
        "leaf": "external_effect",
        "metric_family": "external_effect",
        "scope": scope or "other",
        "comparison_basis": _external_comparison_basis(clause),
        "temporal_basis": _external_temporal_basis(match),
        "cash_basis": "not_applicable",
        "group_id": group_id,
        "category": _external_category(clause, effect_kind),
        "effect_kind": effect_kind,
        "effect_basis": _external_effect_basis(match),
        "recipient_path": recipient_path,
        "qualifiers": _external_qualifiers(sentence, effect_kind),
        "compatibility": "incompatible" if reasons else "compatible",
        "incompatibility_reasons": reasons,
    }
    duration_days = _external_duration_days(clause, current, metric, period)
    if duration_days is not None:
        tags["duration_days"] = duration_days
    return {
        "value": value * sign,
        "unit": unit,
        "period": period or "unresolved",
        "evidence": sentence,
        "source": "report_text",
        "concept": f"text:external_{effect_kind}",
        "relationship_tags": tags,
    }


def _extract_external_effect_facts(
    text: str,
    current: dict[str, dict[str, Any]],
    periods: list[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Extract bounded explicit quantified effects without interpreting subjects."""
    del (
        periods
    )  # Do not invent a period when neither clause nor recipient supplies one.
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text[:_MAX_EXTERNAL_EFFECT_TEXT_CHARS])
    trigger = re.compile(r"\b(?:contribut|drag|headwind|impact|reclassif)", re.I)
    candidate_sentences = [
        (index, sentence)
        for index, sentence in enumerate(sentences)
        if trigger.search(sentence)
    ][:_MAX_EXTERNAL_EFFECT_SENTENCES]
    output: dict[str, dict[str, Any]] = {}
    for sentence_index, raw_sentence in candidate_sentences:
        sentence = " ".join(raw_sentence.split())
        if not sentence or len(sentence) > _MAX_EXTERNAL_EFFECT_SENTENCE_CHARS:
            continue
        evidence = sentence
        if sentence_index + 1 < len(sentences):
            attached = " ".join(sentences[sentence_index + 1].split())
            if re.match(r"^this net impact includes\b", attached, re.I):
                evidence = f"{sentence} {attached}"
        group_id = (
            "external_"
            + hashlib.sha256(
                f"external-effect:{sentence_index}:{sentence.casefold()}".encode()
            ).hexdigest()[:16]
        )
        clauses = re.split(r"\s*(?:;|,\s+(?:and|while|but)\s+)\s*", sentence)
        for clause in clauses[:_MAX_EXTERNAL_EFFECT_CLAUSES]:
            for effect_kind, pattern in _EXTERNAL_EFFECT_PATTERNS:
                for match in pattern.finditer(clause):
                    recipient_label = match.group("recipient")
                    if effect_kind == "reclassification":
                        legs = (
                            (
                                recipient_label,
                                -1,
                                match.groupdict().get("recipient_scope"),
                                True,
                            ),
                            (
                                match.group("recipient_to"),
                                1,
                                match.groupdict().get("recipient_to_scope"),
                                True,
                            ),
                        )
                    else:
                        legs = (
                            (
                                recipient_label,
                                1,
                                match.groupdict().get("recipient_scope"),
                                False,
                            ),
                        )
                    for label, sign, recipient_scope, leg_specific in legs:
                        leaf = _external_effect_leaf(
                            match=match,
                            effect_kind=effect_kind,
                            sentence=evidence,
                            clause=clause,
                            group_id=group_id,
                            current=current,
                            recipient_label=label,
                            sign=sign,
                            recipient_scope=recipient_scope,
                            use_clause_scope=not leg_specific,
                        )
                        if leaf is not None:
                            output[f"external_effect_{len(output) + 1}"] = leaf
                        if len(output) >= _MAX_EXTERNAL_EFFECT_FACTS:
                            return output
    return output


def _text_metric(label: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
    for metric, labels in _TEXT_LABELS:
        if any(
            normalized == candidate or normalized.startswith(candidate + " ")
            for candidate in labels
        ):
            return metric
    return None


def _text_currency_scale(
    lines: list[str],
    index: int,
    statement_start: int,
) -> tuple[str | None, float, str | None]:
    nearby = " ".join(lines[max(0, index - 160) : index + 1])
    upper_nearby = nearby.upper()
    currency = None
    for token, code in _CURRENCY_CODES.items():
        present = token in nearby if len(token) == 1 else token in upper_nearby
        if present:
            currency = code
            break
    if currency is None:
        match = re.search(r"\b(GBP|USD|EUR|CAD|AUD|CHF|JPY)\b", nearby, re.I)
        currency = match.group(1).upper() if match else None
    scale = 1.0
    scale_label = None
    if re.search(r"\b(?:million|millions|m)\b", nearby, re.I):
        scale, scale_label = 1.0, "m"
    elif re.search(r"\b(?:thousand|thousands|k)\b", nearby, re.I):
        scale, scale_label = 0.001, "k"
    return currency, scale, scale_label


def _unavailable_report_text_with_external(
    text: str, base_meta: dict[str, Any], reason: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    external = _extract_external_effect_facts(text, {}, ())
    base_meta.update(
        {
            "status": "success" if external else "unavailable",
            "reason": reason,
            "deterministic_metric_count": len(external),
            "extracted_fact_count": len(external),
            "fact_count": len(external),
            "external_effect_fact_count": len(external),
        }
    )
    return external, {}, base_meta


def extract_report_text_facts(
    extracted_text: str | bytes,
    report_period: Any = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Extract annual facts from layout-preserving OCR/plain report text."""
    text = (
        extracted_text.decode("utf-8", "replace")
        if isinstance(extracted_text, bytes)
        else str(extracted_text or "")
    )
    lines = [
        line.rstrip("\r")
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    anchor_indexes = [
        i for i, line in enumerate(lines) if _STATEMENT_ANCHOR_RE.search(line)
    ]
    base_meta = {
        "source": "report_text",
        "status": "unavailable",
        "deterministic_metric_count": 0,
        "extracted_fact_count": 0,
        "fact_count": 0,
        "statement_anchor_count": len(anchor_indexes),
    }
    if not anchor_indexes:
        return _unavailable_report_text_with_external(
            text, base_meta, "missing_financial_statement_anchor"
        )
    year_counts: Counter[str] = Counter()
    for index in anchor_indexes:
        for line in lines[index : min(len(lines), index + 10)]:
            year_counts.update(
                year
                for year in _YEAR_RE.findall(line)
                if int(year) <= date.today().year + 1
            )
    periods = sorted(year_counts, reverse=True)[:2]
    period_source = "statement_header"
    if len(periods) < 2:
        report_date = (
            report_period
            if isinstance(report_period, date)
            else _iso(str(report_period or ""))
        )
        if report_date is None:
            return _unavailable_report_text_with_external(
                text, base_meta, "missing_current_prior_periods"
            )
        periods = [str(report_date.year), str(report_date.year - 1)]
        period_source = "document_report_date"
    # Two adjacent period columns are required unless OCR damaged the header and
    # the authoritative document date supplies the annual current/prior pair.
    header_ok = any(
        len(_YEAR_RE.findall(line)) >= 2
        for anchor in anchor_indexes
        for line in lines[anchor : min(len(lines), anchor + 10)]
    )
    if not header_ok and period_source == "statement_header":
        return _unavailable_report_text_with_external(
            text, base_meta, "missing_aligned_period_columns"
        )
    candidates: dict[str, list[dict[str, Any]]] = {}
    for index, original in enumerate(lines):
        plain = re.sub(r"<[^>]+>", " ", original)
        match: re.Match[str] | None = None
        metric: str | None = None
        matched_label = ""
        for candidate_metric, labels in _TEXT_LABELS:
            for label in sorted(labels, key=len, reverse=True):
                candidate = re.match(
                    r"^\s*" + re.escape(label) + r"(?:[.:])?(?:\s{2,}|\t+)(.*\S)\s*$",
                    plain,
                    re.I,
                )
                if candidate:
                    match = candidate
                    metric = candidate_metric
                    matched_label = label
                    break
            if match:
                break
        if not match or metric is None:
            continue
        # Require the row to be in a statement block with two aligned values.
        value_text = match.group(1)
        first_number = _NUM_RE.search(value_text)
        if first_number is None or re.search(
            r"[A-Za-z]{2,}", value_text[: first_number.start()]
        ):
            continue
        # A single small leading integer is accepted only as a statement note.
        nearest_anchor = max(
            (anchor for anchor in anchor_indexes if anchor <= index), default=-999
        )
        if nearest_anchor < 0 or index - nearest_anchor > 80:
            continue
        number_matches = list(_NUM_RE.finditer(value_text))
        if len(number_matches) >= 3:
            note = number_matches[0].group(0).strip()
            if re.fullmatch(r"\d{1,2}", note):
                number_matches = number_matches[1:]
        total_header = any(
            len(re.findall(r"\btotal\b", line, re.I)) >= 2
            for line in lines[nearest_anchor : min(len(lines), nearest_anchor + 12)]
        )
        if len(number_matches) > 2 and total_header and "|" in match.group(1):
            segment_numbers = [
                list(_NUM_RE.finditer(segment)) for segment in match.group(1).split("|")
            ]
            segment_numbers = [items for items in segment_numbers if items]
            if len(segment_numbers) == 2:
                number_matches = [segment_numbers[0][-1], segment_numbers[1][-1]]
        if len(number_matches) > 2 and len(number_matches) % 2 == 0 and total_header:
            group_size = len(number_matches) // 2
            number_matches = [
                number_matches[group_size - 1],
                number_matches[-1],
            ]
        if len(number_matches) != 2:
            continue
        pair = _parse_number_pair(
            number_matches[0].group(0), number_matches[1].group(0)
        )
        if pair is None:
            continue
        values = list(pair)
        if metric in {
            "capex",
            "lease_inclusive_investment",
            "inventory",
            "cash",
            "total_debt",
            "total_assets",
            "total_liabilities",
            "current_assets",
            "current_liabilities",
            "shares_outstanding",
        }:
            values = [abs(float(value)) for value in values]
        currency, scale, scale_label = _text_currency_scale(
            lines, index, nearest_anchor
        )
        if metric in _MONETARY_METRICS and currency is None and scale_label is None:
            continue
        if metric in _MONETARY_METRICS:
            unit = f"{currency}m" if currency else "report_millions"
            values = [float(value) * scale for value in values]
        elif metric == "shares_outstanding":
            unit = (
                "million shares"
                if re.search(
                    r"\b(?:million|m)\s+shares\b",
                    " ".join(lines[max(0, index - 3) : index + 1]),
                    re.I,
                )
                else "shares"
            )
        elif metric == "diluted_eps":
            if currency is None:
                continue
            unit = f"{currency}/share"
        else:
            continue
        for period, value in zip(periods[:2], values, strict=False):
            candidate = {
                "value": value,
                "unit": unit,
                "period": period,
                "evidence": original,
                "source": "report_text",
                "concept": f"text:{matched_label}",
            }
            scope = (
                "consolidated"
                if re.match(r"^\s*consolidated\b", lines[nearest_anchor], re.I)
                else "other"
            )
            _tag_relationship_metric(metric, candidate, scope=scope)
            candidates.setdefault(metric, []).append(candidate)
    current: dict[str, dict[str, Any]] = {}
    prior: dict[str, dict[str, Any]] = {}
    for metric, values in candidates.items():
        by_period: dict[str, list[dict[str, Any]]] = {}
        for value in values:
            by_period.setdefault(value["period"], []).append(value)
        if any(
            len({(item["value"], item["unit"]) for item in items}) != 1
            for items in by_period.values()
        ):
            continue
        current_value = by_period.get(periods[0], [])
        prior_value = by_period.get(periods[1], [])
        if (
            metric
            in {
                "revenue",
                "gross_profit",
                "total_assets",
                "total_liabilities",
                "equity",
                "current_assets",
                "current_liabilities",
            }
            and current_value
            and prior_value
        ):
            current_number = abs(float(current_value[0]["value"]))
            prior_number = abs(float(prior_value[0]["value"]))
            if (
                current_number
                and prior_number
                and not (1 / 3) <= current_number / prior_number <= 3.0
            ):
                continue
        if current_value:
            current[metric] = current_value[0]
        if prior_value:
            prior[metric] = prior_value[0]
    _derive_report_metrics(current, prior, "report_text")
    external_effects = _extract_external_effect_facts(text, current, periods[:2])
    current.update(external_effects)
    count = len(current)
    base_meta.update(
        {
            "status": "success" if count else "unavailable",
            "deterministic_metric_count": count,
            "extracted_fact_count": (
                sum(len(values) for values in candidates.values())
                + len(external_effects)
            ),
            "fact_count": (
                sum(len(values) for values in candidates.values())
                + len(external_effects)
            ),
            "external_effect_fact_count": len(external_effects),
            "periods": periods[:2],
            "period_source": period_source,
        }
    )
    if not count:
        base_meta["reason"] = (
            "no_supported_statement_rows"
            if not candidates
            else "conflicting_statement_values"
        )
    return current, prior, base_meta


def _merge_external_text_facts(
    current: dict[str, dict[str, Any]],
    metadata: dict[str, Any],
    extracted_text: Any,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not isinstance(extracted_text, (str, bytes)) or not extracted_text:
        return current, metadata
    text = (
        extracted_text.decode("utf-8", "replace")
        if isinstance(extracted_text, bytes)
        else extracted_text
    )
    periods = metadata.get("periods")
    external = _extract_external_effect_facts(
        text,
        current,
        periods if isinstance(periods, (list, tuple)) else (),
    )
    if not external:
        return current, metadata
    current.update(external)
    metadata = dict(metadata)
    metadata.update(
        {
            "deterministic_metric_count": len(current),
            "extracted_fact_count": int(metadata.get("extracted_fact_count") or 0)
            + len(external),
            "fact_count": int(metadata.get("fact_count") or 0) + len(external),
            "external_effect_fact_count": len(external),
        }
    )
    return current, metadata


def extract_document_facts(
    document: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Extract non-SEC report facts from raw bytes or layout-preserving text."""
    raw = document.get("raw_content")
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
        current, prior, metadata = extract_ixbrl_facts(raw_bytes)
        if metadata.get("status") == "success":
            current, metadata = _merge_external_text_facts(
                current, metadata, document.get("extracted_text")
            )
            return current, prior, metadata
        if not document.get("extracted_text") and raw_bytes:
            return extract_report_text_facts(raw_bytes, document.get("report_date"))
    elif isinstance(raw, str) and raw:
        current, prior, metadata = extract_ixbrl_facts(raw)
        if metadata.get("status") == "success":
            current, metadata = _merge_external_text_facts(
                current, metadata, document.get("extracted_text")
            )
            return current, prior, metadata
        if not document.get("extracted_text"):
            return extract_report_text_facts(raw, document.get("report_date"))
    text = document.get("extracted_text")
    if text:
        return extract_report_text_facts(text, document.get("report_date"))
    return (
        {},
        {},
        {
            "source": "report",
            "status": "unavailable",
            "reason": "missing_content",
            "deterministic_metric_count": 0,
        },
    )


extract_report_facts = extract_document_facts
extract_non_sec_facts = extract_document_facts
extract_non_sec_report_facts = extract_document_facts
extract_annual_report_facts = extract_document_facts


def load_deterministic_facts(
    config: dict[str, Any], document: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Fetch standardized SEC facts or parse a non-SEC report document."""
    if document.get("filing_source") != "sec_edgar":
        if document.get("raw_content") or document.get("extracted_text"):
            return extract_document_facts(document)
        return {}, {}, {"source": "none", "status": "unsupported"}
    cik = sec_cik(document)
    if not cik:
        return (
            {},
            {},
            {"source": "sec_xbrl", "status": "unavailable", "reason": "missing_cik"},
        )
    user_agent = config.get("investment_filings", {}).get(
        "sec_user_agent",
        "TradingDataInvestmentResearch/1.0 (research@trading-data-platform.local)",
    )
    try:
        response = make_request(
            "GET",
            SEC_COMPANYFACTS_URL.format(cik=cik),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=45.0,
            max_retries=2,
            client=get_shared_client(),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("SEC Companyfacts response was not an object")
    except Exception as exc:
        return (
            {},
            {},
            {
                "source": "sec_xbrl",
                "status": "unavailable",
                "reason": type(exc).__name__,
            },
        )
    current, prior, metadata = extract_sec_facts(document, payload)
    if metadata.get("status") == "success":
        current, metadata = _merge_external_text_facts(
            current, metadata, document.get("extracted_text")
        )
    return current, prior, metadata
