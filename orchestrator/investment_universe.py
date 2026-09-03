"""Static market-cap universe for automated regulatory filing intake."""

from __future__ import annotations

import json
from pathlib import Path

_UNIVERSE_JSON_PATH = Path(__file__).resolve().parent / "data" / "universe.json"

with open(_UNIVERSE_JSON_PATH, encoding="utf-8") as _f:
    _DATA = json.load(_f)

UNIVERSE_SNAPSHOT_DATE: str = _DATA["snapshot_date"]
UNIVERSE_SOURCE: str = _DATA["source"]
EU_UNIVERSE_SOURCE: str = _DATA["eu_source"]

_ALL_COMPANIES: list[dict] = _DATA["companies"]
TOP_US_COMPANIES: tuple[dict, ...] = tuple(c for c in _ALL_COMPANIES if c.get("market") == "US")
TOP_UK_COMPANIES: tuple[dict, ...] = tuple(c for c in _ALL_COMPANIES if c.get("market") == "UK")
TOP_EU_COMPANIES: tuple[dict, ...] = tuple(c for c in _ALL_COMPANIES if c.get("market") == "EU")

_COMPANY_BY_SYMBOL: dict[str, str] = {
    c["symbol"]: c["company"] for c in _ALL_COMPANIES if "symbol" in c and "company" in c
}

ISSUER_INDUSTRIES: tuple[tuple[str, str, str], ...] = tuple(
    (sym, _COMPANY_BY_SYMBOL.get(sym, ""), ind)
    for sym, ind in _DATA["industry_overrides"].items()
)

ISSUER_INDUSTRY_LABELS: dict[str, str] = dict(_DATA["industry_labels"])
_LEGACY_INDUSTRY_LABELS: dict[str, str] = dict(_DATA["legacy_industry_labels"])
ISSUER_ALIASES: dict[str, tuple[str, ...]] = {
    sym: tuple(aliases) for sym, aliases in _DATA["aliases"].items()
}


def _normalize_company_name(value: object) -> str:
    name = " ".join(str(value).split()).casefold()
    if name.endswith("'s"):
        name = name[:-2]
    return name


def _canonical_industry_label(value: object) -> str:
    label = " ".join(str(value or "").split()).strip()
    return _LEGACY_INDUSTRY_LABELS.get(label, label)


_ISSUER_INDUSTRY_BY_SYMBOL: dict[str, str] = {}
_ISSUER_INDUSTRY_BY_COMPANY: dict[str, str] = {}


def _build_issuer_industry_index() -> None:
    """Index canonical industries for every configured issuer."""
    for symbol, industry in ISSUER_INDUSTRY_LABELS.items():
        _ISSUER_INDUSTRY_BY_SYMBOL[str(symbol).strip().upper()] = industry
    for symbol, _company, industry in ISSUER_INDUSTRIES:
        _ISSUER_INDUSTRY_BY_SYMBOL[str(symbol).strip().upper()] = industry
    for company in TOP_EU_COMPANIES:
        industry = _canonical_industry_label(company.get("industry"))
        symbol = company.get("symbol")
        if industry and industry != "Unclassified" and symbol:
            _ISSUER_INDUSTRY_BY_SYMBOL.setdefault(str(symbol).strip().upper(), industry)
    for company in (*TOP_US_COMPANIES, *TOP_UK_COMPANIES, *TOP_EU_COMPANIES):
        name = company.get("company")
        symbol = company.get("symbol")
        if not name or not symbol:
            continue
        industry = _ISSUER_INDUSTRY_BY_SYMBOL.get(str(symbol).strip().upper())
        if industry:
            _ISSUER_INDUSTRY_BY_COMPANY[_normalize_company_name(name)] = industry


_build_issuer_industry_index()


def industry_for(symbol: object = None, company: object = None) -> str:
    """Return the checked-in canonical industry for a configured issuer.

    Resolution prefers the symbol, then the exact normalized company
    identity. Every configured issuer in the built-in universe resolves to
    one of the eight concrete canonical categories; unknown issuers fail
    closed to "Unclassified" rather than guessing, so classification never
    fabricates an industry.
    """
    if symbol:
        industry = _ISSUER_INDUSTRY_BY_SYMBOL.get(str(symbol).strip().upper())
        if industry:
            return industry
    if company:
        industry = _ISSUER_INDUSTRY_BY_COMPANY.get(_normalize_company_name(company))
        if industry:
            return industry
    return "Unclassified"


def top_us_uk_eu_companies() -> list[dict]:
    """Return fresh mappings so callers cannot mutate the static universe."""
    return [
        dict(company)
        for company in (*TOP_US_COMPANIES, *TOP_UK_COMPANIES, *TOP_EU_COMPANIES)
    ]


def configured_region_counts() -> dict[str, int]:
    """Count configured issuers by canonical dashboard region."""
    counts = {"US": 0, "EU": 0, "ASIA": 0}
    for universe in (TOP_US_COMPANIES, TOP_UK_COMPANIES, TOP_EU_COMPANIES):
        for company in universe:
            region = company.get("region")
            if region in counts:
                counts[region] += 1
    return counts
