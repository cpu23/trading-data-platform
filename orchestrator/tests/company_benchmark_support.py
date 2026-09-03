"""Shared fixtures, helper builders, and constants for company benchmark tests."""

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

import investment_service as service  # noqa: E402
from research_intelligence import company_benchmarks as cb  # noqa: E402
from research_intelligence import company_judging as judging  # noqa: E402

EXCERPT = "AI demand remained durable while supply stayed tight."
NEWS_ITEM = {
    "title": "Chip demand steady",
    "available_at": "2026-03-01T00:00:00Z",
    "published_at": "2026-03-01T00:00:00Z",
}


def producer_raw():
    return {
        "schema_version": cb.SCHEMA_VERSION,
        "case_id": "MU.FY25.Q3",
        "fixture_version": 1,
        "as_of": "2026-03-31T00:00:00Z",
        "document": {
            "company": "Micron Technology",
            "symbol": "MU",
            "document_type": "annual_report",
            "region": "US",
            "industry": "Semiconductors & Compute",
            "report_date": "2026-02-28",
            "available_at": "2026-03-30T00:00:00Z",
        },
        "excerpt": EXCERPT,
        "deterministic_current": {},
        "deterministic_prior": {},
        "market_inputs": {},
        "prior_facts": {},
        "previous_state": None,
        "prior_count": 0,
        "news_items": [dict(NEWS_ITEM)],
        "extraction": {"report_text_source": "stored_document"},
    }


def evaluator_raw(fingerprint, **overrides):
    raw = {
        "schema_version": cb.SCHEMA_VERSION,
        "case_id": "mu.fy25.q3",
        "fixture_version": 1,
        "producer_fingerprint": fingerprint,
        "expected_material_observations": ["Data-centre demand quantified"],
        "deterministic_checks": [],
        "strongest_counter_thesis": "Demand reverses in H2.",
        "expected_unknowns": [],
        "known_traps": [],
        "later_outcomes": [],
        "required_material_evidence": [],
        "forbidden_hindsight": [],
    }
    raw.update(overrides)
    return raw


def write_yaml(directory, name, payload):
    path = Path(directory) / name
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def not_disclosed_materiality():
    return {
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
    }


def narrative_payload(
    summary="Demand durable, supply tight.",
    thesis=None,
    *,
    counter_thesis="The thesis fails if durable demand reverses.",
    materiality_assessment=None,
    document_type="annual_report",
    sector="Technology",
    industry="Semiconductors",
    region="US",
    confidence="high",
    qualitative=None,
    drivers=None,
    catalysts=None,
    risks=None,
    relationship_reconciliations=None,
    watch_items=None,
    numeric_claims=None,
):
    """Valid response whose only present evidence sits inside ``EXCERPT``."""
    if qualitative is None:
        qualitative = {
            name: {"present": False, "strength": "none", "evidence": ""}
            for name in service.QUALITATIVE_NAMES
        }
        qualitative["ai_demand"] = {
            "present": True,
            "strength": "strong",
            "evidence": "demand remained durable",
        }
    payload = {
        "classification": {
            "document_type": document_type,
            "sector": sector,
            "industry": industry,
            "region": region,
            "confidence": confidence,
        },
        "qualitative": qualitative,
        "summary": summary,
        "thesis": thesis if thesis is not None else "Thesis holds unless orders reverse.",
        "counter_thesis": counter_thesis,
        "materiality_assessment": (
            materiality_assessment
            if materiality_assessment is not None
            else not_disclosed_materiality()
        ),
        "drivers": list(drivers) if drivers is not None else [],
        "catalysts": list(catalysts) if catalysts is not None else [],
        "risks": list(risks) if risks is not None else [],
        "relationship_reconciliations": (
            list(relationship_reconciliations)
            if relationship_reconciliations is not None
            else []
        ),
        "watch_items": list(watch_items) if watch_items is not None else [],
    }
    if numeric_claims is not None:
        payload["numeric_claims"] = list(numeric_claims)
    return payload

def executor_identity(execution_id):
    """Exact executor identity block required in stage_config."""
    return {
        "executor_kind": "agent_environment",
        "execution_id": execution_id,
        "executor_name": "benchmark-agent-runner",
        "executor_version": "1.4.2",
    }


EXECUTOR_IDENTITY = executor_identity("exec-2026-04-01-mu-fy25-q3-001")
OTHER_EXECUTOR_IDENTITY = executor_identity("exec-2026-04-01-av-fy25-q1-002")
BAD_FIRST_PASS = '{"classification": {"document_type": "annual_repor'
REPAIR_PROMPT = (
    "Repair the JSON once. Return only a complete replacement object that "
    "matches the original strict schema."
)
FAILING_LEDGER_ROW = {
    "check_id": "chk_revenue",
    "kind": "number_close",
    "path": "facts.metrics.revenue.value",
    "severity": "material",
    "rationale": "revenue must print within tolerance",
    "expected": 150,
}


def judge_payload(
    request,
    *,
    overall=5,
    score=5,
    defects=(),
):
    """Strict-schema blind-judge response bound to exactly ``request``."""
    return {
        "role": request.role,
        "token": request.token,
        "prompt_version": request.prompt_version,
        "response_binding": request.response_binding,
        "overall": overall,
        "dimension_scores": {
            dimension: {
                "score": score,
                "rationale": f"{dimension} graded against the rubric anchors",
            }
            for dimension in judging.JUDGE_DIMENSIONS
        },
        "concrete_defects": list(defects),
        "severe_regression": False,
        "severe_regression_reason": None,
        "abstained": False,
        "abstention_reason": None,
    }


def other_producer_raw():
    """A second, distinct company case for cross-mixing experiments."""
    raw = producer_raw()
    raw["case_id"] = "AV.FY25.Q1"
    raw["document"] = dict(raw["document"], company="Aurora Ventures", symbol="AV")
    return raw


def other_narrative_payload():
    """Distinct valid response for the second case's accepted attempt."""
    payload = narrative_payload()
    payload["summary"] = "Orders softened while inventory stayed lean."
    payload["thesis"] = "Thesis holds only if bookings stabilise."
    return payload


def narrative_payload_for_request(request, *, alternate=False):
    def relationship_fact_clause(fact):
        finance_display_labels = {
            "cash_paid_for_property_and_equipment": "cash capital expenditures",
        }
        metric_label = fact["metric_label"]
        metric = finance_display_labels.get(
            metric_label,
            metric_label.replace("_", " "),
        )
        value = fact["value"]
        unit = fact["unit"]
        numeric_display = {
            "percent": f"{value}%",
            "percentage_points": f"{value} percentage points",
            "usd_billions": f"{value} billion USD",
            "usd_millions": f"{value} million USD",
            "usd_per_share": f"{value} USD per share",
            "ratio": f"{value}x",
            "count": f"{value} count",
        }[unit]
        return f"{metric} was {numeric_display} in {fact['period']}."

    payload = other_narrative_payload() if alternate else narrative_payload()
    rows = []
    numeric_claims = list(payload.get("numeric_claims", ()))
    summary_syntheses = []
    thesis_syntheses = []
    selected_summary_facts = set()
    labels = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")
    for index, relationship in enumerate(request.material_relationships):
        label = labels[index] if index < len(labels) else f"rel_{index}"
        compatible = relationship["compatibility"] == "compatible"
        fact_clauses = []
        required_refs = relationship["required_facts"]
        for fact_index, ref in enumerate(required_refs):
            fact_key = ref["fact_path"].removeprefix("deterministic_current.relationship_facts.")
            fact = request.relationship_facts.get(ref["fact_path"]) or request.relationship_facts[fact_key]
            fact_clauses.append(relationship_fact_clause(fact))
            numeric_claims.append(
                {
                    "claim_id": (
                        f"relationship-{index}-fact-{fact_index}-observation"
                    ),
                    "path": (
                        f"$.relationship_reconciliations[{index}].observation"
                    ),
                    "value": fact["value"],
                    "metric": fact["metric_label"],
                    "period": fact["period"],
                    "unit": fact["unit"],
                    "currency": fact["currency"],
                    "source_kind": "fact",
                    "fact_path": ref["fact_path"],
                }
            )

        selected_refs = required_refs[:1] if compatible else []
        selected_clauses = fact_clauses[: len(selected_refs)]
        summary_synthesis = " ".join(selected_clauses)
        thesis_synthesis = (
            f"Relationship interpretation {label} informs the thesis."
            if compatible
            else ""
        )
        for ref in selected_refs:
            fact_path = ref["fact_path"]
            if fact_path in selected_summary_facts:
                continue
            selected_summary_facts.add(fact_path)
            fact_key = fact_path.removeprefix("deterministic_current.relationship_facts.")
            fact = request.relationship_facts.get(fact_path) or request.relationship_facts[fact_key]
            numeric_claims.append(
                {
                    "claim_id": f"relationship-summary-{len(selected_summary_facts)}",
                    "path": "summary",
                    "value": fact["value"],
                    "metric": fact["metric_label"],
                    "period": fact["period"],
                    "unit": fact["unit"],
                    "currency": fact["currency"],
                    "source_kind": "fact",
                    "fact_path": fact_path,
                }
            )

        rows.append(
            {
                "relationship_id": relationship["relationship_id"],
                "status": (
                    "reconciled" if compatible else "abstained_incompatible"
                ),
                "fact_paths": [ref["fact_path"] for ref in required_refs],
                "observation": (
                    " ".join(fact_clauses)
                    if fact_clauses
                    else f"Relationship observation {label}."
                ),
                "interpretation": thesis_synthesis,
                "uncertainty": f"Relationship uncertainty {label} remains.",
                "summary_synthesis": summary_synthesis,
                "thesis_synthesis": thesis_synthesis,
                "summary_fact_paths": [
                    ref["fact_path"] for ref in selected_refs
                ],
            }
        )
        if summary_synthesis and summary_synthesis not in summary_syntheses:
            summary_syntheses.append(summary_synthesis)
        if thesis_synthesis and thesis_synthesis not in thesis_syntheses:
            thesis_syntheses.append(thesis_synthesis)

    payload["relationship_reconciliations"] = rows
    if numeric_claims:
        payload["numeric_claims"] = numeric_claims
    payload["summary"] = " ".join([payload["summary"], *summary_syntheses])
    payload["thesis"] = " ".join([payload["thesis"], *thesis_syntheses])
    return payload

def relationship_metric(value, *, role, family):
    return {
        "value": value,
        "unit": "percent",
        "period": "FY2025",
        "evidence": ["demand remained durable"],
        "source": "reported",
        "relationship_tags": {
            "role": role,
            "metric_family": family,
            "leaf": "growth",
            "scope": "consolidated",
            "comparison_basis": "year_over_year_gaap",
            "temporal_basis": "rate_over_period",
            "cash_basis": "not_applicable",
        },
    }


def relationship_producer_raw():
    raw = producer_raw()
    raw["deterministic_current"] = {
        "revenue_growth": relationship_metric(
            8.0,
            role="top_line",
            family="revenue",
        ),
        "net_income_growth": relationship_metric(
            5.0,
            role="bottom_line",
            family="net_income",
        ),
    }
    return raw

def _frozen_structure_problems(value, path="packet"):
    """Report every nested container that is not a proxy-over-copy or tuple."""
    problems = []
    if isinstance(value, MappingProxyType):
        for key, item in value.items():
            problems.extend(_frozen_structure_problems(item, f"{path}.{key}"))
    elif isinstance(value, Mapping):
        problems.append(f"{path} is a mutable {type(value).__name__}")
    elif isinstance(value, tuple):
        for index, item in enumerate(value):
            problems.extend(_frozen_structure_problems(item, f"{path}[{index}]"))
    elif isinstance(value, list):
        problems.append(f"{path} is a mutable list")
    return problems


def _iter_mutation_attempts(value):
    """Yield one callable per frozen container; calling it must raise."""
    if isinstance(value, MappingProxyType):

        def break_mapping(container=value):
            container["immutability-probe"] = "mutated"

        yield break_mapping
        for item in value.values():
            yield from _iter_mutation_attempts(item)
    elif isinstance(value, tuple):
        if value:

            def break_sequence(container=value):
                container[0] = "mutated"

            yield break_sequence
        for item in value:
            yield from _iter_mutation_attempts(item)

def _put(container, path, value):
    """Build a ``producer_raw`` mutator setting ``value`` at a dotted path."""
    steps = path.split(".")

    def mutate(raw):
        updated = dict(raw[container])
        raw[container] = updated
        node = updated
        for step in steps[:-1]:
            child = dict(node.get(step) or {})
            node[step] = child
            node = child
        node[steps[-1]] = value

    return mutate

def _judge_round_for(producer, evaluator, finalized, blind_salt):
    """Rebuild requests and strictly parse each raw response in salt order."""
    requests = judging.build_blind_judge_requests(
        producer, evaluator, finalized, blind_salt
    )
    results = [
        judging.parse_judge_result(request, judge_payload(request))
        for request in requests
    ]
    records = [
        {
            "role": request.role,
            "token": request.token,
            "raw_json": json.dumps(judge_payload(request)),
            "execution_id": f"judge-exec-{index}",
            "session_id": f"judge-session-{index}",
            "provenance": {},
        }
        for index, request in enumerate(requests)
    ]
    return requests, results, records


def _finalized_for(case, payload):
    """Finalize a caller payload decorated with the request's v7 contract."""
    request_payload = narrative_payload_for_request(cb.prepare_company_run(case))
    generated_rows = request_payload["relationship_reconciliations"]
    caller_rows = payload.get("relationship_reconciliations")
    reconciliations = caller_rows if caller_rows else generated_rows

    decorated = dict(payload)
    decorated["relationship_reconciliations"] = reconciliations
    decorated["numeric_claims"] = [
        *payload.get("numeric_claims", []),
        *request_payload.get("numeric_claims", []),
    ]
    relationship_summary = request_payload["summary"].removeprefix(
        narrative_payload()["summary"]
    ).strip()
    decorated["summary"] = " ".join(
        part for part in (payload["summary"], relationship_summary) if part
    )
    decorated["thesis"] = " ".join(
        [
            payload["thesis"],
            *(
                row.get("thesis_synthesis") or row.get("interpretation")
                for row in generated_rows
                if row.get("thesis_synthesis") or row.get("interpretation")
            ),
        ]
    )
    recorded = cb.recorded_executor_output(
        json.dumps(decorated), {"model": "recorded-model"}
    )
    return cb.finalize_recorded_company_run(recorded, case)
