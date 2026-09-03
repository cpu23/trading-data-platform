"""Shared support fixtures and helpers for investment service tests."""

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import investment_service as service


@contextmanager
def session_context(session):
    yield session


def metric(value, unit="USDm", period="FY2025", evidence="report evidence"):
    return {"value": value, "unit": unit, "period": period, "evidence": evidence}


def sec_index_page(*rows):
    """Build an EDGAR ``*-index.htm`` Document Format Files table body.

    ``rows`` are ``(document_name, doc_type)`` pairs.
    """
    body = "".join(
        f"<tr><td>{index}</td><td>{doc_type}</td>"
        f"<td><a href='/Archives/edgar/data/x/{name}'>{name}</a></td>"
        f"<td>{doc_type}</td><td>1000</td></tr>"
        for index, (name, doc_type) in enumerate(rows, start=1)
    )
    return (
        "<html><body><table class='tableFile' summary='Document Format Files'>"
        "<tr><th scope='col'>Seq</th><th scope='col'>Description</th>"
        "<th scope='col'>Document</th><th scope='col'>Type</th>"
        "<th scope='col'>Size</th></tr>" + body + "</table></body></html>"
    )


def sec_directory_fake_request(index_response, index_page_response, primary_response):
    """Route SEC recovery requests: index.json, then ``*-index.htm``, then the
    selected primary document."""

    def route(method, url, **kwargs):
        if url.endswith("index.json"):
            return index_response
        if "-index.htm" in url or "-index.html" in url:
            return index_page_response
        return primary_response

    return route


def investment_report_payload():
    """Build one schema-valid investment-report model payload."""
    qualitative = {
        name: {"present": False, "strength": "none", "evidence": ""}
        for name in service.QUALITATIVE_NAMES
    }
    qualitative["ai_demand"] = {
        "present": True,
        "strength": "strong",
        "evidence": "Demand remained durable",
    }
    return {
        "classification": {
            "document_type": "annual report",
            "sector": "Technology",
            "industry": "Semiconductors",
            "region": "US",
            "confidence": "high",
        },
        "qualitative": qualitative,
        "summary": "Revenue rose while demand held.",
        "thesis": "Thesis stands unless orders reverse.",
        "counter_thesis": "Orders could reverse and invalidate the thesis.",
        "materiality_assessment": {
            topic: {
                "status": "not_disclosed",
                "observation": "",
                "implication": "",
                "evidence": "",
            }
            for topic in (
                "forward_guidance",
                "reported_variance_driver",
                "margin_economics",
                "capital_commitment_duration",
            )
        },
        "drivers": [],
        "catalysts": [],
        "risks": [],
        "relationship_reconciliations": [],
        "watch_items": [],
        "numeric_claims": [],
    }


def epistemic_risk():
    return {
        "sourced_observation": "Demand remained durable",
        "inference": "Order volume may remain resilient",
        "epistemic_state": "supported",
        "uncertainty": "The duration of demand is uncertain",
        "likelihood": "medium",
        "impact": "high",
        "mitigation": "Monitor order conversion",
        "evidence": "Demand remained durable",
    }


def epistemic_catalyst():
    return {
        "trigger": "Demand remained durable",
        "expected_outcome": "Order volume may remain resilient",
        "horizon": "within the next year",
        "epistemic_state": "supported",
        "uncertainty": "The duration of demand is uncertain",
        "evidence": "Demand remained durable",
    }


def relationship_metric(value, *, role, metric_family, cash_basis):
    return {
        "value": value,
        "unit": "usd_millions",
        "currency": "USD",
        "period": "FY2025",
        "evidence": ["Neutral filing disclosure"],
        "source": "reported",
        "relationship_tags": {
            "role": role,
            "metric_family": metric_family,
            "leaf": "standard_metric",
            "scope": "consolidated",
            "comparison_basis": "none",
            "temporal_basis": "period_flow",
            "cash_basis": cash_basis,
        },
    }

