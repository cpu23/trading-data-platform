"""Deterministic company, industry, and macro classification for news."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

MAX_FEED_BYTES = 2_000_000

# Keep this taxonomy deterministic and shared by every investment endpoint.
KEY_INDUSTRIES: tuple[str, ...] = (
    "Semiconductors & Compute",
    "Software, Cloud & Communications",
    "Energy & Utilities",
    "Industrials & Materials",
    "Financials & Real Estate",
    "Healthcare",
    "Consumer",
    "Aerospace & Defence",
)
ALL_INDUSTRIES: tuple[str, ...] = KEY_INDUSTRIES + ("Unclassified",)

INDUSTRY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Semiconductors & Compute",
        (
            "semiconductor",
            "semiconductors",
            "semiconductors & memory",
            "chipmaker",
            "chip making",
            "foundry",
            "dram",
            "nand",
            "memory chip",
            "processor",
            "gpu",
            "compute",
            "asic",
            "silicon",
            "electronic",
            "electronics",
            "connector",
            "connectors",
            "data storage",
            "storage",
        ),
    ),
    (
        "Software, Cloud & Communications",
        (
            "software",
            "saas",
            "cloud",
            "data center",
            "data centers",
            "data centre",
            "data centres",
            "datacenter",
            "cloud infrastructure",
            "ai infrastructure",
            "ai infrastructure & data centres",
            "cybersecurity",
            "telecom",
            "communications",
            "networking",
            "internet",
            "information technology",
            "technology",
            "computer software",
            "programming",
        ),
    ),
    (
        "Energy & Utilities",
        (
            "energy",
            "oil",
            "oilfield",
            "petroleum",
            "drilling",
            "subsea",
            "natural gas",
            "gas",
            "lng",
            "utility",
            "utilities",
            "power grid",
            "electricity",
            "renewable",
            "solar",
            "wind power",
            "nuclear",
        ),
    ),
    (
        "Industrials & Materials",
        (
            "industrial",
            "industrials",
            "automation",
            "robot",
            "robotics",
            "machinery",
            "factory equipment",
            "materials",
            "steel",
            "copper",
            "metal",
            "mining",
            "construction",
            "building",
            "railroad",
            "railway",
            "equipment",
            "industrials, automation & robotics",
        ),
    ),
    (
        "Financials & Real Estate",
        (
            "financial",
            "bank",
            "banks",
            "insurer",
            "insurance",
            "capital market",
            "asset manager",
            "asset management",
            "private equity",
            "credit market",
            "fintech",
            "payment",
            "payments",
            "broker",
            "broking",
            "savings",
            "investment",
            "wealth",
            "real estate",
            "property",
            "banks, insurance & capital markets",
        ),
    ),
    (
        "Healthcare",
        (
            "healthcare",
            "health care",
            "biotech",
            "biological",
            "pharma",
            "drugmaker",
            "clinical trial",
            "medical device",
            "hospital",
            "surgical",
            "orthopedic",
            "therapeutics",
        ),
    ),
    (
        "Consumer",
        (
            "consumer",
            "consumer spending",
            "retailer",
            "retail sales",
            "retail",
            "e-commerce",
            "ecommerce",
            "beverage",
            "beverages",
            "restaurant",
            "tobacco",
            "education",
            "hotel",
            "hospitality",
            "entertainment",
            "household",
            "automobile",
            "automotive",
        ),
    ),
    (
        "Aerospace & Defence",
        (
            "aerospace",
            "defence contractor",
            "defense contractor",
            "arms maker",
            "missile manufacturer",
            "fighter jet",
            "military contractor",
            "defence",
            "defense",
            "aircraft",
            "aviation",
        ),
    ),
)

INDUSTRY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Semiconductors & Compute",
        (
            "semiconductor",
            "chipmaker",
            "chip making",
            "foundry",
            "dram",
            "nand",
            "memory chip",
            "processor",
            "gpu",
            "asic",
        ),
    ),
    (
        "Software, Cloud & Communications",
        (
            "software",
            "saas",
            "cloud",
            "data center",
            "data centre",
            "datacenter",
            "cloud infrastructure",
            "ai infrastructure",
            "cybersecurity",
            "telecom",
            "communications",
            "networking",
        ),
    ),
    (
        "Energy & Utilities",
        (
            "oil",
            "oilfield",
            "petroleum",
            "drilling",
            "subsea",
            "natural gas",
            "lng",
            "utility",
            "power grid",
            "electricity",
            "renewable",
            "solar",
            "wind power",
            "nuclear",
        ),
    ),
    (
        "Industrials & Materials",
        (
            "industrial",
            "automation",
            "robot",
            "robotics",
            "machinery",
            "factory equipment",
            "steel",
            "copper",
            "metal",
            "mining",
            "construction equipment",
            "railroad",
        ),
    ),
    (
        "Financials & Real Estate",
        (
            "bank",
            "insurer",
            "insurance",
            "capital market",
            "asset manager",
            "private equity",
            "credit market",
            "fintech",
            "payment network",
            "brokerage",
            "real estate",
            "reit",
        ),
    ),
    (
        "Healthcare",
        (
            "healthcare",
            "health care",
            "biotech",
            "pharma",
            "drugmaker",
            "clinical trial",
            "medical device",
            "hospital",
            "therapeutics",
        ),
    ),
    (
        "Consumer",
        (
            "consumer spending",
            "retailer",
            "retail sales",
            "e-commerce",
            "ecommerce",
            "beverage",
            "restaurant",
            "hotel",
            "automobile",
        ),
    ),
    (
        "Aerospace & Defence",
        (
            "aerospace",
            "defence contractor",
            "defense contractor",
            "arms maker",
            "missile manufacturer",
            "fighter jet",
            "military contractor",
            "aircraft manufacturer",
        ),
    ),
)

THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "artificial_intelligence",
        (
            "artificial intelligence",
            "generative ai",
            " ai ",
            "gpu",
            "large language model",
        ),
    ),
    (
        "capital_spending",
        ("capital spending", "capital expenditure", "capex", "investment plan"),
    ),
    (
        "pricing_inflation",
        ("inflation", "pricing power", "price increase", "input costs", "tariff"),
    ),
    (
        "rates_credit",
        (
            "interest rate",
            "rate cut",
            "rate hike",
            "bond yield",
            "credit spread",
            "loan demand",
            "default",
        ),
    ),
    (
        "energy_transition",
        (
            "energy transition",
            "renewable",
            "electric vehicle",
            "battery",
            "power grid",
            "nuclear",
        ),
    ),
    (
        "supply_chain",
        ("supply chain", "shortage", "inventory", "capacity", "export control"),
    ),
    (
        "consumer_demand",
        (
            "consumer demand",
            "consumer spending",
            "retail sales",
            "discretionary spending",
        ),
    ),
    (
        "geopolitics_trade",
        ("sanction", "trade war", "tariff", "export control", "geopolitical", "war in"),
    ),
    (
        "regulation",
        (
            "regulator",
            "regulation",
            "antitrust",
            "competition authority",
            "sec investigation",
        ),
    ),
    (
        "earnings_guidance",
        (
            "earnings",
            "profit warning",
            "guidance",
            "revenue forecast",
            "sales forecast",
        ),
    ),
    (
        "deals_capital",
        ("merger", "acquisition", "takeover", "buyback", "share repurchase", "ipo"),
    ),
)

MACRO_THEMES = frozenset(
    {
        "capital_spending",
        "pricing_inflation",
        "rates_credit",
        "energy_transition",
        "supply_chain",
        "consumer_demand",
        "geopolitics_trade",
    }
)


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _values(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item, 80) for item in value[:limit] if _clean(item, 80)]


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.casefold()
    if phrase.startswith(" ") or phrase.endswith(" "):
        return phrase in f" {text} "
    return bool(
        re.search(
            rf"(?<!\w){re.escape(phrase)}(?!\w)",
            text,
        )
    )


def canonicalize_industry(value: Any) -> str:
    """Map supported labels and deterministic aliases to the strict taxonomy."""
    cleaned = _clean(value, 120)
    normalized = cleaned.casefold()
    for industry in ALL_INDUSTRIES:
        if normalized == industry.casefold():
            return industry
    for industry, aliases in INDUSTRY_ALIASES:
        if any(_contains(normalized, alias) for alias in aliases):
            return industry
    return "Unclassified"


def classify_news_item(
    item: dict[str, Any],
    companies: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Return bounded deterministic categories with explicit ambiguity state."""
    title = _clean(item.get("title"), 240)
    summary = _clean(item.get("summary"), 600)
    tags = _values(item.get("tags"))
    supplied_symbols = {value.upper() for value in _values(item.get("symbols"))}
    text = " ".join((title, summary, *tags)).casefold()

    industries = [
        industry
        for industry, phrases in INDUSTRY_RULES
        if any(_contains(text, phrase) for phrase in phrases)
    ]
    themes = [
        theme
        for theme, phrases in THEME_RULES
        if any(_contains(text, phrase) for phrase in phrases)
    ]
    matched_companies: list[str] = []
    matched_symbols: list[str] = []
    seen = set()
    for company in companies:
        name = _clean(company.get("company"), 160)
        symbol = _clean(company.get("symbol"), 24).upper()
        name_hit = len(name) >= 4 and name.casefold() in text
        symbol_hit = symbol in supplied_symbols or (
            len(symbol) >= 2
            and re.search(
                rf"(?<![A-Z0-9])\${re.escape(symbol)}(?![A-Z0-9])",
                title.upper(),
            )
        )
        if not name_hit and not symbol_hit:
            continue
        identity = symbol or name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        matched_companies.append(name)
        if symbol:
            matched_symbols.append(symbol)
        company_industry = canonicalize_industry(company.get("industry"))
        if company_industry not in industries:
            industries.append(company_industry)

    ambiguity = (
        "unclassified"
        if not industries and not themes and not matched_companies
        else "ambiguous"
        if len(industries) > 2
        else "classified"
    )
    return {
        "id": _clean(item.get("id"), 200),
        "source": _clean(item.get("source_label") or item.get("source"), 64),
        "title": title,
        "summary": summary,
        "url": _clean(item.get("url"), 2048),
        "published": _clean(item.get("published"), 64),
        "companies": matched_companies[:8],
        "symbols": matched_symbols[:8],
        "industries": industries[:5],
        "themes": themes[:6],
        "macro_relevant": bool(MACRO_THEMES.intersection(themes)),
        "classification_method": "deterministic_keywords_entities",
        "ambiguity": ambiguity,
    }


def load_classified_news(
    config: dict[str, Any],
    companies: Iterable[dict[str, Any]] = (),
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read the bounded feed and classify newest items deterministically."""
    output = config.get("news_feed", {}).get(
        "output_path", "/var/lib/trading-data/news"
    )
    path = os.path.join(output, "feed.json")
    try:
        if os.path.getsize(path) > MAX_FEED_BYTES:
            return []
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    items = payload.get("items", []) if isinstance(payload, dict) else []
    company_list = list(companies)
    return [
        classify_news_item(item, company_list)
        for item in items[: max(0, min(limit, 500))]
        if isinstance(item, dict)
    ]


def published_timestamp(item: dict[str, Any]) -> float | None:
    raw = item.get("published")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).timestamp()
    except ValueError:
        return None
