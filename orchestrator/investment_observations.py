"""Persistent company, industry, and news observations for investment research."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from db import get_session
from investment_news import classify_news_item, published_timestamp
from investment_universe import top_us_uk_eu_companies

_REPORT_UPSERT = text(
    """
    INSERT INTO investment_research_observations (
        source_kind, source_id, observed_at, industry, company, symbol, region,
        metrics, narrative, score, state, provenance
    ) VALUES (
        'report', :source_id, :observed_at, :industry, :company, :symbol, :region,
        CAST(:metrics AS JSONB), CAST(:narrative AS JSONB), :score, :state,
        CAST(:provenance AS JSONB)
    )
    ON CONFLICT (source_kind, source_id, industry) DO UPDATE SET
        observed_at = EXCLUDED.observed_at,
        company = EXCLUDED.company,
        symbol = EXCLUDED.symbol,
        region = EXCLUDED.region,
        metrics = EXCLUDED.metrics,
        narrative = EXCLUDED.narrative,
        score = EXCLUDED.score,
        state = EXCLUDED.state,
        provenance = EXCLUDED.provenance,
        updated_at = NOW()
    """
)
_REPORT_STALE_DELETE = text(
    """
    DELETE FROM investment_research_observations
    WHERE source_kind = 'report'
      AND source_id = :source_id
      AND industry <> :industry
    """
)


_NEWS_UPSERT = text(
    """
    INSERT INTO investment_research_observations (
        source_kind, source_id, observed_at, industry, company, symbol,
        metrics, narrative, themes, state, provenance
    ) VALUES (
        'news', :source_id, :observed_at, :industry, :company, :symbol,
        '{}'::JSONB, CAST(:narrative AS JSONB), :themes, :state,
        CAST(:provenance AS JSONB)
    )
    ON CONFLICT (source_kind, source_id, industry) DO UPDATE SET
        observed_at = EXCLUDED.observed_at,
        company = EXCLUDED.company,
        symbol = EXCLUDED.symbol,
        narrative = EXCLUDED.narrative,
        themes = EXCLUDED.themes,
        state = EXCLUDED.state,
        provenance = EXCLUDED.provenance,
        updated_at = NOW()
    """
)
_FINANCIAL_REPORT_TYPES = frozenset(
    {"annual_report", "quarterly_report", "earnings_release"}
)


def _observation_metrics(
    facts: dict[str, Any], analysis: dict[str, Any]
) -> dict[str, Any]:
    """Flatten deterministic report, ratio, and valuation history."""
    analysis_metrics = analysis.get("metrics")
    fact_metrics = facts.get("metrics")
    metrics = dict(
        analysis_metrics
        if isinstance(analysis_metrics, dict)
        else fact_metrics
        if isinstance(fact_metrics, dict)
        else {}
    )

    fundamentals = analysis.get("fundamentals")
    if isinstance(fundamentals, dict):
        for name, raw in fundamentals.items():
            value = _finite(raw)
            if value is None:
                continue
            metrics[f"fundamental_{name}"] = {
                "value": value,
                "unit": "percent" if str(name).endswith("_pct") else "ratio",
                "source": "deterministic_analysis",
            }

    valuation = analysis.get("valuation")
    if isinstance(valuation, dict):
        valuation_fields = {
            "pe_ratio": ("ratio", valuation.get("pe_ratio", valuation.get("pe"))),
            "fcf": ("currency", valuation.get("fcf")),
            "market_cap": ("currency", valuation.get("market_cap")),
            "market_price": ("currency_per_share", valuation.get("market_price")),
            "dcf_per_share": ("currency_per_share", valuation.get("dcf_per_share")),
            "intrinsic_value": (
                "currency_per_share",
                valuation.get("intrinsic_value"),
            ),
            "margin_of_safety": ("ratio", valuation.get("margin_of_safety")),
        }
        for name, (unit, raw) in valuation_fields.items():
            value = _finite(raw)
            if value is None:
                continue
            metrics[f"valuation_{name}"] = {
                "value": value,
                "unit": unit,
                "source": "deterministic_analysis",
            }
    return metrics


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def upsert_report_observation(
    session,
    document: dict[str, Any],
    facts: dict[str, Any],
    analysis: dict[str, Any],
    *,
    model: str,
) -> None:
    """Write one normalized financial-report observation transactionally."""
    if document.get("document_type") not in _FINANCIAL_REPORT_TYPES:
        return
    report_date = document.get("report_date")
    observed_at = (
        datetime.combine(report_date, datetime.min.time(), tzinfo=UTC)
        if hasattr(report_date, "year") and not isinstance(report_date, datetime)
        else report_date or datetime.now(UTC)
    )
    state = analysis.get("state")
    if not isinstance(state, str):
        state = (
            analysis.get("stage") if isinstance(analysis.get("stage"), str) else None
        )
    industry = str(document.get("industry") or "Unclassified")
    session.execute(
        _REPORT_STALE_DELETE,
        {"source_id": str(document["document_id"]), "industry": industry},
    )
    session.execute(
        _REPORT_UPSERT,
        {
            "source_id": str(document["document_id"]),
            "observed_at": observed_at,
            "industry": industry,
            "company": document.get("company"),
            "symbol": document.get("symbol"),
            "region": document.get("region"),
            "metrics": json.dumps(_observation_metrics(facts, analysis)),
            "narrative": json.dumps(
                {
                    "summary": analysis.get("summary"),
                    "thesis": analysis.get("thesis"),
                    "counter_thesis": analysis.get("counter_thesis"),
                    "materiality_assessment": analysis.get("materiality_assessment") or facts.get("materiality_assessment") or {},
                    "qualitative": facts.get("qualitative", {}),
                    "drivers": analysis.get("drivers", []),
                    "catalysts": analysis.get("catalysts", []),
                    "risks": analysis.get("risks", []),
                    "relationship_facts": analysis.get("relationship_facts"),
                    "material_relationships": analysis.get("material_relationships"),
                    "relationship_reconciliations": analysis.get(
                        "relationship_reconciliations"
                    ),
                    "watch_items": analysis.get("watch_items", []),
                    "news_context": analysis.get("news_context", []),
                }
            ),
            "score": _finite(analysis.get("score")),
            "state": state,
            "provenance": json.dumps(
                {
                    "document_id": str(document["document_id"]),
                    "document_type": document.get("document_type"),
                    "filing_source": document.get("filing_source"),
                    "model": model,
                    "extraction": analysis.get("extraction", {}),
                }
            ),
        },
    )


def _news_source_id(item: dict[str, Any]) -> str:
    supplied = str(item.get("id") or "").strip()
    if supplied:
        return supplied
    identity = "\n".join(
        str(item.get(key) or "") for key in ("source", "url", "title", "published")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def persist_news_observations(
    config: dict[str, Any], items: Iterable[dict[str, Any]]
) -> int:
    """Classify and idempotently log published news for historical monitoring."""
    companies = top_us_uk_eu_companies()
    params: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = classify_news_item(raw, companies)
        published = published_timestamp(item)
        observed_at = (
            datetime.fromtimestamp(published, UTC)
            if published is not None
            else datetime.now(UTC)
        )
        industries = item.get("industries") or ["Unclassified"]
        source_id = _news_source_id(item)
        for industry in industries:
            params.append(
                {
                    "source_id": source_id,
                    "observed_at": observed_at,
                    "industry": industry,
                    "company": item.get("companies", [None])[0]
                    if item.get("companies")
                    else None,
                    "symbol": item.get("symbols", [None])[0]
                    if item.get("symbols")
                    else None,
                    "narrative": json.dumps(
                        {
                            key: item.get(key)
                            for key in (
                                "source",
                                "title",
                                "summary",
                                "url",
                                "companies",
                                "symbols",
                                "industries",
                                "themes",
                                "macro_relevant",
                                "ambiguity",
                                "classification_method",
                            )
                        }
                    ),
                    "themes": list(item.get("themes") or []),
                    "state": item.get("ambiguity"),
                    "provenance": json.dumps(
                        {
                            "news_id": source_id,
                            "source": item.get("source"),
                            "classification_method": item.get("classification_method"),
                        }
                    ),
                }
            )
    if not params:
        return 0
    with get_session(config) as session:
        session.execute(_NEWS_UPSERT, params)
    return len(params)


def load_observations(
    config: dict[str, Any], *, limit: int = 5000
) -> list[dict[str, Any]]:
    with get_session(config) as session:
        rows = session.execute(
            text(
                """
                SELECT source_kind, source_id, observed_at, industry, company,
                       symbol, region, metrics, narrative, themes, score, state,
                       provenance
                FROM investment_research_observations
                ORDER BY observed_at DESC, source_kind, source_id
                LIMIT :limit
                """
            ),
            {"limit": max(1, min(limit, 10000))},
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def aggregate_industry_history(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return auditable daily report/news history without model aggregation."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        observed_at = row.get("observed_at")
        date_key = (
            observed_at.date().isoformat()
            if hasattr(observed_at, "date")
            else str(observed_at)[:10]
        )
        industry = str(row.get("industry") or "Unclassified")
        bucket = grouped.setdefault(
            (industry, date_key),
            {
                "reports": set(),
                "news": set(),
                "companies": set(),
                "scores": [],
                "themes": Counter(),
                "metric_facts": 0,
            },
        )
        source_id = str(row.get("source_id") or "")
        if row.get("source_kind") == "report":
            bucket["reports"].add(source_id)
            company_id = str(row.get("symbol") or row.get("company") or "").casefold()
            if company_id:
                bucket["companies"].add(company_id)
            score = _finite(row.get("score"))
            if score is not None:
                bucket["scores"].append(score)
            metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
            bucket["metric_facts"] += sum(
                isinstance(value, dict) and value.get("value") is not None
                for value in metrics.values()
            )
        else:
            bucket["news"].add(source_id)
            bucket["themes"].update(row.get("themes") or [])
    by_industry: dict[str, list[dict[str, Any]]] = {}
    for (industry, date_key), bucket in grouped.items():
        scores = bucket["scores"]
        by_industry.setdefault(industry, []).append(
            {
                "date": date_key,
                "report_count": len(bucket["reports"]),
                "company_count": len(bucket["companies"]),
                "news_count": len(bucket["news"]),
                "deterministic_metric_count": bucket["metric_facts"],
                "average_score": round(sum(scores) / len(scores), 2)
                if scores
                else None,
                "themes": [name for name, _ in bucket["themes"].most_common(5)],
            }
        )
    return [
        {
            "industry": industry,
            "points": sorted(points, key=lambda item: item["date"])[-48:],
        }
        for industry, points in sorted(by_industry.items())
    ]
