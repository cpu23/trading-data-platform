"""Shared support fixtures and test base classes for company quality tests."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import investment_service as service
import yaml
from research_intelligence import company_benchmarks as cb
from research_intelligence import company_quality as cq

EXCERPT = (
    "AI demand remained durable while supply stayed tight. Revenue rose 12 percent."
)
NEWS_ITEM = {
    "title": "Chip demand steady",
    "available_at": "2026-03-01T00:00:00Z",
    "published_at": "2026-03-01T00:00:00Z",
}
MSFT_EXCERPT = (
    "This quarter, revenue was $64.7 billion, up 15% and 16% in constant "
    "currency. Azure and other cloud services revenue grew 29% and 30% in "
    "constant currency. Capital expenditures including finance leases were "
    "$19 billion in FY2024 Q4, in line with expectations, and cash paid "
    "for P, P, and E was $13.9 billion. Free cash flow was $23.3 billion, "
    "up 18% year-over-year."
)
ARITHMETIC_METRICS = {
    "revenue": {"value": 200},
    "shares_outstanding": {"value": 100},
    "eps": {"value": 2},
}
ARITHMETIC_ROW = {
    "check_id": "chk_eps",
    "kind": "arithmetic_close",
    "path": "facts.metrics.revenue.value",
    "severity": "material",
    "rationale": "r",
    "numerator_path": "facts.metrics.revenue.value",
    "denominator_path": "facts.metrics.shares_outstanding.value",
    "scale": 1,
    "expected_path": "facts.metrics.eps.value",
    "tolerance": 0.01,
}


def producer_raw(excerpt=None, deterministic_current=None):
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
        "excerpt": excerpt if excerpt is not None else EXCERPT,
        "deterministic_current": (
            deterministic_current if deterministic_current is not None else {}
        ),
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
        "expected_material_observations": [],
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
        "thesis": thesis
        if thesis is not None
        else "Thesis holds unless orders reverse.",
        "counter_thesis": counter_thesis,
        "materiality_assessment": (
            materiality_assessment
            if materiality_assessment is not None
            else {
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


def epistemic_catalyst(trigger, horizon, evidence):
    return {
        "trigger": trigger,
        "expected_outcome": "The disclosed operating trend becomes measurable.",
        "horizon": horizon,
        "epistemic_state": "supported",
        "uncertainty": "The timing and magnitude remain uncertain.",
        "evidence": evidence,
    }


def finalized_for(payload, **overrides):
    arguments = {
        "document": dict(producer_raw()["document"]),
        "deterministic_current": {},
        "deterministic_prior": {},
        "market_inputs": {},
        "stored_previous_facts": {},
        "previous_state": None,
        "prior_count": 0,
        "news_items": [dict(NEWS_ITEM)],
        "extraction": {},
        "relationship_facts": {},
        "material_relationships": (),
    }
    arguments.update(overrides)
    return service.finalize_investment_analysis(copy.deepcopy(payload), **arguments)


def ledger_row(kind, path, expected, *, severity="material", rationale="r"):
    return {
        "check_id": f"chk_{kind}",
        "kind": kind,
        "path": path,
        "severity": severity,
        "rationale": rationale,
        "expected": expected,
    }


def nonblank_ledger_row(path, *, severity="material", rationale="r"):
    """Nonblank rows carry exactly the common keys — never ``expected``."""
    return {
        "check_id": "chk_nonblank",
        "kind": "nonblank",
        "path": path,
        "severity": severity,
        "rationale": rationale,
    }


def msft_claim_row(**overrides):
    """A ledger row for the canonical $19B quarterly capex claim.

    Text rows always carry the verbatim producer quote: structural row
    validation rejects any text row without one, and this default quote is
    verbatim inside ``MSFT_EXCERPT``, including its source-carried period.
    """
    row = {
        "claim_id": "capex_fy24q4",
        "path": "summary",
        "value": "$19B",
        "metric": "capital expenditures including finance leases",
        "period": "FY2024 Q4",
        "unit": "usd_billions",
        "currency": "USD",
        "source_kind": "text",
        "quote": (
            "Capital expenditures including finance leases were $19 "
            "billion in FY2024 Q4, in line with expectations"
        ),
    }
    row.update(overrides)
    return row


class NumericClaimBindingTestBase(unittest.TestCase):
    """Base test case for numeric claim binding gate tests."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    def _producer(self, **overrides):
        raw = producer_raw(excerpt=overrides.pop("excerpt", MSFT_EXCERPT))
        raw.update(overrides)
        return cb.load_producer_case(write_yaml(self.directory, "producer.yaml", raw))

    def _evaluator(self, producer, **overrides):
        return cb.load_evaluator_case(
            write_yaml(
                self.directory,
                "evaluator.yaml",
                evaluator_raw(producer.fingerprint, **overrides),
            ),
            producer=producer,
        )

    def _payload(self, summary, rows):
        # The shared helper's default ai_demand evidence quote does not sit
        # inside this suite's Microsoft excerpt, so the narrative carries a
        # quote that does — keeping every other gate green by design.
        payload = narrative_payload(summary=summary)
        payload["qualitative"]["ai_demand"]["evidence"] = "revenue was $64.7 billion"
        if rows is not None:
            payload["numeric_claims"] = rows
        return payload

    def _run(
        self,
        producer=None,
        evaluator=None,
        summary="",
        rows=None,
        deterministic_current=None,
    ):
        if deterministic_current is not None:
            # The frozen case and the finalization must see the SAME
            # deterministic facts: build the producer with them before any
            # evaluator pairing, then reuse the identical mapping at
            # finalization so `.value` fact pointers resolve.
            producer = self._producer(deterministic_current=deterministic_current)
        producer = producer or self._producer()
        evaluator = evaluator or self._evaluator(producer)
        finalized = finalized_for(
            self._payload(summary, rows),
            deterministic_current=deterministic_current or {},
        )
        return cq.run_company_hard_gates(producer, evaluator, finalized)

    def _codes(self, report, code):
        return [failure for failure in report.failures if failure.code == code]

    def _run_payload_with_json_replay(
        self, payload, producer=None, deterministic_current=None
    ):
        if deterministic_current is not None:
            producer = producer or self._producer(
                deterministic_current=deterministic_current
            )
        producer = producer or self._producer()
        evaluator = self._evaluator(producer)
        finalized = finalized_for(
            payload,
            deterministic_current=deterministic_current or {},
        )
        replay_blob = json.loads(
            json.dumps(
                {
                    "facts": finalized.facts,
                    "classified_industry": finalized.classified_industry,
                    "previous_facts": finalized.previous_facts,
                    "analysis": finalized.analysis,
                }
            )
        )
        replayed = service.InvestmentFinalizedAnalysis(**replay_blob)
        direct_report = cq.run_company_hard_gates(producer, evaluator, finalized)
        replay_report = cq.run_company_hard_gates(producer, evaluator, replayed)
        self.assertEqual(replay_report, direct_report)
        return direct_report

    def _run_with_json_replay(
        self, *, summary, rows, producer=None, deterministic_current=None
    ):
        return self._run_payload_with_json_replay(
            self._payload(summary, rows),
            producer=producer,
            deterministic_current=deterministic_current,
        )

    def _microsoft_document(self, *, title=None, report_date="2024-06-30"):
        document = dict(producer_raw()["document"])
        document.update(
            {
                "company": "Microsoft Corporation",
                "symbol": "MSFT",
                "document_type": "earnings_call",
                "report_date": report_date,
                "available_at": "2024-07-30T21:00:00Z",
            }
        )
        if title is not None:
            document["title"] = title
        return document

    def _rpo_row(self, *, quote=None, period="FY2024-Q4"):
        return {
            "claim_id": "commercial-rpo",
            "path": "summary",
            "value": "$269 billion",
            "metric": "remaining performance obligation",
            "period": period,
            "unit": "usd_billions",
            "currency": "USD",
            "source_kind": "text",
            "quote": quote or "Remaining performance obligation was $269 billion",
        }

    def _target_domain_outcomes(self, payload):
        producer = self._producer()
        try:
            service._validated_investment_facts(
                json.dumps(payload),
                excerpt=producer.excerpt,
                news_items=[dict(item) for item in producer.news_items],
                deterministic_current=service._freeze_json_value({}),
                deterministic_prior=service._freeze_json_value({}),
                relationship_facts=service._freeze_json_value({}),
                material_relationships=(),
            )
        except service.InvestmentValidationError as error:
            live_passed = False
            live_failure_code = error.category
        else:
            live_passed = True
            live_failure_code = None

        finalized = finalized_for(payload)
        report = cq.run_company_hard_gates(
            producer,
            self._evaluator(producer),
            finalized,
        )
        return live_passed, live_failure_code, report
