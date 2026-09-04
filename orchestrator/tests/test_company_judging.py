"""Unit tests for the blind three-role company judging seam.

Exercises ``research_intelligence.company_judging`` end to end over pure
in-process fixtures: request construction (count, determinism, salt
sensitivity, blindness, recursive schema freezing), strict result parsing
(accept/reject matrix), and panel aggregation (identity binding,
medians/minima, threshold and hard-gate boundaries). Regressions pin the
non-compensable invariants: malformed hard-gate reports fail closed without
raising while reports lacking or carrying a foreign ``producer_fingerprint``
raise outright as cross-run mixups, dataclass-like gate reports are consumed
structurally, the per-judge core-dimension floor of 4.0 fails any judge
scoring below it even when medians pass, and dispatch materialization
never aliases stored request state. Producer/evaluator/finalized inputs
are minimal shape-compatible stand-ins copied locally so the heavy
production modules stay unimported. No mocks of the functions under test;
no I/O.

Scores are decimal: the overall and every dimension accept integers or
values on the exact 0.1 grid across 1.0..5.0. The boundary/table suites
pin tenth-step acceptance, overprecision/type/range/nonfinite rejection,
exact-threshold panel reachability (4.3, 4.5, 4.8) with 0.1-below
failures, strict sub-4.0 rationale/defect enforcement, and decimal
fidelity through result fields, medians, and report serialization
without integer coercion.

Response binding: every request carries a distinct, salt-bound, producer-bound
64-hex ``response_binding`` rendered verbatim into its prompt; the strict
parser accepts only responses echoing that exact value and exposes it on the
result. The canonical request ``fingerprint`` stays a digest over the exact
request (including its rendered prompt), is never echoed by judges or results,
and never appears inside the prompt itself.
"""

import copy
import hashlib
import json
import sys
import unittest
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from research_intelligence.company_benchmarks import ForbiddenCompanyClaim
from research_intelligence.company_judging import (
    CORE_DIMENSION_JUDGE_FLOOR,
    COUNTER_THESIS_STRENGTH_MEDIAN_MINIMUM,
    DIMENSION_MEDIAN_MINIMUM,
    FACTUAL_FIDELITY_MEDIAN_MINIMUM,
    JUDGE_DIMENSIONS,
    JUDGE_ROLES,
    MATERIALITY_MEDIAN_MINIMUM,
    OVERALL_MEDIAN_MINIMUM,
    PROMPT_VERSION,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    SCORE_MAXIMUM,
    SCORE_MINIMUM,
    BlindJudgeRequest,
    JudgePanelReport,
    aggregate_judge_panel,
    build_blind_judge_requests,
    parse_judge_result,
)
from research_intelligence.contracts import canonical_fingerprint

AS_OF = datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
SALT_A = "run-salt-alpha"
PRODUCER_IDENTITY = "8a05f2c980d877b1c21cd8efa59cd595cff1b57aa8c3b1a542b6a2ecf7d6b828"
SALT_B = "run-salt-beta"
OTHER_IDENTITY = "c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6008a05f2c980d877b1c21cde"
EXCERPT = (
    "Revenue grew 12% year over year while gross margin held at 41%. "
    "Management guided next-quarter revenue between 3.1 and 3.4 billion."
)
EVIDENCE = "Management guided next-quarter revenue between 3.1 and 3.4 billion."


# ---------------------------------------------------------------------------
# Minimal local stand-ins (shape-compatible with the production dataclasses,
# which company_judging consumes structurally via typing only).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProducerCase:
    schema_version: str
    case_id: str
    fixture_version: int
    as_of: datetime
    document: dict
    excerpt: str
    deterministic_current: dict
    deterministic_prior: dict
    market_inputs: dict
    prior_facts: dict
    previous_state: str | None
    prior_count: int
    news_items: tuple
    extraction: dict
    fingerprint: str
    producer_fingerprint: str
    source_path: str


@dataclass(frozen=True)
class EvaluatorCase:
    schema_version: str
    case_id: str
    fixture_version: int
    producer_fingerprint: str
    expected_material_observations: tuple
    deterministic_checks: tuple
    strongest_counter_thesis: str
    expected_unknowns: tuple
    known_traps: tuple
    later_outcomes: tuple
    required_material_evidence: tuple
    forbidden_hindsight: tuple
    source_path: str


@dataclass(frozen=True)
class Finalized:
    facts: dict
    classified_industry: str
    previous_facts: dict | None
    analysis: dict


def producer_case(**overrides) -> ProducerCase:
    base = {
        "schema_version": "company_producer_case_v1",
        "case_id": "acme_fy25",
        "fixture_version": 1,
        "as_of": AS_OF,
        "document": {"document_type": "annual_report", "report_date": "2025-12-31"},
        "excerpt": EXCERPT,
        "deterministic_current": {
            "revenue": {
                "value": 3400000000.0,
                "unit": "usd",
                "period": "2025-12-31",
                "evidence": "Revenue grew 12% year over year",
            }
        },
        "deterministic_prior": {},
        "market_inputs": {"risk_free_rate": 0.042},
        "prior_facts": {},
        "extraction": {"report_text_source": "stored_document"},
        "previous_state": None,
        "prior_count": 2,
        "news_items": (),
        "fingerprint": PRODUCER_IDENTITY,
        "producer_fingerprint": PRODUCER_IDENTITY,
        "source_path": "cases/acme_fy25.json",
    }
    base.update(overrides)
    return ProducerCase(**base)


def evaluator_case() -> EvaluatorCase:
    return EvaluatorCase(
        schema_version="company_evaluator_case_v1",
        case_id="acme_fy25",
        fixture_version=1,
        producer_fingerprint=PRODUCER_IDENTITY,
        expected_material_observations=("gross margin resilience",),
        deterministic_checks=(),
        strongest_counter_thesis="guidance cut drives multiple compression",
        expected_unknowns=("actual Q1 demand mix",),
        known_traps=(),
        later_outcomes=("quarter printed above the guide",),
        required_material_evidence=(EVIDENCE,),
        forbidden_hindsight=(
            ForbiddenCompanyClaim(
                claim_id="capex_q1",
                metric_aliases=("capex",),
                value=20,
                period_aliases=("Q1 FY2025",),
                available_after=AS_OF,
            ),
        ),
        source_path="cases/acme_fy25.eval.json",
    )


def finalized_analysis() -> Finalized:
    evidence = "Management guided next-quarter revenue between 3.1 and 3.4 billion."
    counter_thesis = "Pricing power erodes as customer budgets compress."
    materiality_assessment = {
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
    return Finalized(
        facts={
            "classification": {
                "document_type": "annual_report",
                "sector": "Technology",
                "industry": "Software Infrastructure",
                "region": "US",
                "confidence": "moderate",
            },
            "qualitative": {
                "pricing_power": {
                    "present": True,
                    "strength": "moderate",
                    "evidence": "gross margin held at 41%",
                },
                "guidance_up": {
                    "present": True,
                    "strength": "weak",
                    "evidence": evidence,
                },
            },
            "metrics": {
                "revenue": {
                    "value": 3400000000.0,
                    "unit": "usd",
                    "period": "2025-12-31",
                    "evidence": "Revenue grew 12% year over year",
                }
            },
            "counter_thesis": counter_thesis,
            "materiality_assessment": copy.deepcopy(materiality_assessment),
            "relationship_facts": {},
            "material_relationships": [],
            "relationship_reconciliations": [],
        },
        classified_industry="Software Infrastructure",
        previous_facts=None,
        analysis={
            "summary": "Steady growth with resilient margins.",
            "thesis": "Pricing power sustains margins into next year.",
            "counter_thesis": counter_thesis,
            "materiality_assessment": copy.deepcopy(materiality_assessment),
            "catalysts": [
                {
                    "trigger": "Management issues the Q1 guide",
                    "expected_outcome": "Revenue lands within the guided range",
                    "horizon": "next quarter",
                    "epistemic_state": "supported",
                    "uncertainty": "Customer demand may change before quarter end.",
                    "evidence": evidence,
                }
            ],
            "risks": [
                {
                    "sourced_observation": "gross margin held at 41%",
                    "inference": "Demand softness could pressure future margins.",
                    "epistemic_state": "hypothesis",
                    "uncertainty": "Current bookings are not disclosed.",
                    "likelihood": "low",
                    "impact": "medium",
                    "mitigation": "No company mitigation stated; monitor bookings.",
                    "evidence": "gross margin held at 41%",
                }
            ],
            "relationship_facts": {},
            "material_relationships": [],
            "relationship_reconciliations": [],
            "watch_items": ["bookings trend"],
        },
    )


def populated_relationship_finalized_analysis() -> Finalized:
    finalized = finalized_analysis()
    relationship_facts = {
        "rf_revenue_growth": {
            "fact_id": "rf_revenue_growth",
            "metric_key": "revenue_growth",
            "metric_label": "Revenue growth",
            "value": 12.0,
            "unit": "percent",
            "currency": None,
            "period": "FY2025",
            "scope": "consolidated",
            "comparison_basis": "year_over_year_gaap",
            "temporal_basis": "rate_over_period",
            "cash_basis": "not_applicable",
            "source_paths": ["deterministic_current.metrics.revenue"],
            "derivation": "reported",
            "qualifiers": [],
        },
        "rf_net_income_growth": {
            "fact_id": "rf_net_income_growth",
            "metric_key": "net_income_growth",
            "metric_label": "Net income growth",
            "value": 9.0,
            "unit": "percent",
            "currency": None,
            "period": "FY2025",
            "scope": "consolidated",
            "comparison_basis": "year_over_year_gaap",
            "temporal_basis": "rate_over_period",
            "cash_basis": "not_applicable",
            "source_paths": ["deterministic_current.metrics.net_income"],
            "derivation": "reported",
            "qualifiers": [],
        },
    }
    material_relationships = [
        {
            "relationship_id": "mr_revenue_vs_net_income_growth",
            "kind": "same_period_top_bottom_growth",
            "priority": 1,
            "compatibility": "compatible",
            "incompatibility_reasons": [],
            "required_facts": [
                {
                    "fact_path": (
                        "deterministic_current.relationship_facts.rf_revenue_growth"
                    ),
                    "role": "top_line",
                },
                {
                    "fact_path": (
                        "deterministic_current.relationship_facts.rf_net_income_growth"
                    ),
                    "role": "bottom_line",
                },
            ],
        }
    ]
    summary_synthesis = "Revenue growth was 12.0% in FY2025."
    thesis_synthesis = "Earnings growth trailed top-line growth."
    relationship_reconciliations = [
        {
            "relationship_id": "mr_revenue_vs_net_income_growth",
            "status": "reconciled",
            "fact_paths": [
                "deterministic_current.relationship_facts.rf_revenue_growth",
                "deterministic_current.relationship_facts.rf_net_income_growth",
            ],
            "observation": ("Revenue growth of 12% exceeded net income growth of 9%."),
            "interpretation": thesis_synthesis,
            "uncertainty": "The excerpt does not explain the growth gap.",
            "summary_synthesis": summary_synthesis,
            "thesis_synthesis": thesis_synthesis,
            "summary_fact_paths": [
                "deterministic_current.relationship_facts.rf_revenue_growth"
            ],
        }
    ]
    finalized.analysis["summary"] = (
        f"{finalized.analysis['summary']} {summary_synthesis}"
    )
    finalized.analysis["thesis"] = f"{finalized.analysis['thesis']} {thesis_synthesis}"
    for material in (finalized.facts, finalized.analysis):
        material["relationship_facts"] = json.loads(json.dumps(relationship_facts))
        material["material_relationships"] = json.loads(
            json.dumps(material_relationships)
        )
        material["relationship_reconciliations"] = json.loads(
            json.dumps(relationship_reconciliations)
        )
    return finalized


def build_requests(salt: str = SALT_A):
    return build_blind_judge_requests(
        producer_case(),
        evaluator_case(),
        finalized_analysis(),
        salt,
    )


def stated_response_binding(request) -> str:
    """Extract the response binding exactly as the prompt states it.

    Executor-realistic: a blind judge sees only the rendered prompt and must
    echo the binding value from its OUTPUT CONTRACT; this reads that value
    straight out of the prompt text rather than reaching into request state,
    so any drift between prompt and attribute fails these tests loudly.
    """
    marker = "response_binding="
    start = request.prompt.index(marker) + len(marker)
    return request.prompt[start : start + 64]


def valid_payload(request) -> dict:
    """A fully valid, top-scoring judge response bound to ``request``."""
    return {
        "role": request.role,
        "token": request.token,
        "prompt_version": request.prompt_version,
        "response_binding": stated_response_binding(request),
        "overall": 5,
        "dimension_scores": {
            dimension: {
                "score": 5,
                "rationale": f"clear textual support for {dimension}",
            }
            for dimension in JUDGE_DIMENSIONS
        },
        "concrete_defects": ["summary omits the guided revenue range"],
        "severe_regression": False,
        "severe_regression_reason": None,
        "abstained": False,
        "abstention_reason": None,
    }


def tenth_step_values() -> tuple[float, ...]:
    """Every accepted score value on the closed 1.0..5.0 tenth-step grid."""
    return tuple(index / 10 for index in range(10, 51))


def invalid_score_values() -> tuple:
    """Score inputs that must be rejected outright.

    Groups: overprecision beyond one decimal, off-grid decimals, wrong
    types (bool/string/container/None), out-of-range magnitudes on both
    sides, and nonfinite floats. Values here deliberately include shapes a
    naive ``round(x, 1)`` or ``int(x)`` guard would silently accept.
    """
    return (
        # Overprecision beyond one decimal place.
        4.15,
        4.05,
        4.995,
        3.333333,
        2.7182818,
        # Wrong types: bool is int-shaped, str is JSON-text, rest are junk.
        True,
        False,
        "4.3",
        "4",
        None,
        [4.5],
        {"score": 4.5},
        (4.5,),
        # Out of range on both sides of the closed interval.
        0,
        -1,
        100,
        0.9,
        0.95,
        -4.5,
        5.1,
        5.01,
        6.0,
        # Nonfinite.
        float("nan"),
        float("inf"),
        float("-inf"),
    )


def scored_payload(
    request,
    overall,
    dimension_score=None,
    concrete_defects="auto",
) -> dict:
    """Valid payload carrying arbitrary on-grid scores for every field.

    ``overall`` may be an int or a tenth-step float; ``dimension_score``
    (default: same value) applies to every dimension. With the default
    ``concrete_defects="auto"`` a precisely located defect is included
    exactly when the lowest awarded score lands below 4.0, mirroring the
    strict parsing rule; an explicit list overrides that choice.
    """
    if dimension_score is None:
        dimension_score = overall
    if concrete_defects == "auto":
        concrete_defects = (
            ["summary asserts growth absent from the excerpt"]
            if min([overall, dimension_score]) < 4.0
            else []
        )
    return {
        "role": request.role,
        "token": request.token,
        "prompt_version": request.prompt_version,
        "response_binding": stated_response_binding(request),
        "overall": overall,
        "dimension_scores": {
            dimension: {
                "score": dimension_score,
                "rationale": f"{dimension} graded against the rubric anchors",
            }
            for dimension in JUDGE_DIMENSIONS
        },
        "concrete_defects": list(concrete_defects),
        "severe_regression": False,
        "severe_regression_reason": None,
        "abstained": False,
        "abstention_reason": None,
    }


def panel_with_scores(requests, scores_by_dimension) -> object:
    """Aggregate three parsed results with per-judge decimal rescoring.

    ``scores_by_dimension`` maps dimension name to a per-judge triple;
    untouched dimensions stay at the top score. Every judge keeps a
    nonblank rationale, and the base payload already cites one defect, so
    any sub-4.0 entry stays parseable and the panel thresholds alone
    decide the outcome.
    """
    results = []
    for judge_index, request in enumerate(requests):
        payload = valid_payload(request)
        for dimension, triple in scores_by_dimension.items():
            payload["dimension_scores"][dimension] = {
                "score": triple[judge_index],
                "rationale": f"{dimension} graded "
                f"{triple[judge_index]} against the rubric anchors",
            }
        results.append(parse_judge_result(request, payload))
    return aggregate_judge_panel(list(requests), results, passing_gate())


def panel_with_overalls(requests, overalls) -> object:
    """Aggregate three parsed results with per-judge decimal overalls.

    Dimension scores stay at the top score so the named-dimension median
    criteria are untouched: only the overall medians vary.
    """
    results = [
        parse_judge_result(
            request,
            scored_payload(request, overall, dimension_score=5),
        )
        for request, overall in zip(requests, overalls, strict=True)
    ]
    return aggregate_judge_panel(list(requests), results, passing_gate())


def gate_with_identity(producer_fingerprint):
    """A well-formed passing hard-gate report stamped with an identity."""
    return {
        "passed": True,
        "failures": [],
        "producer_fingerprint": producer_fingerprint,
    }


def qualifying_results(requests=None):
    """Three valid top-scoring results: the panel passes every criterion."""
    requests = requests if requests is not None else build_requests()
    return [parse_judge_result(request, valid_payload(request)) for request in requests]


def varied_payload(request, judge_index: int) -> dict:
    """Valid payload with judge-dependent 4/5 spread for median math."""
    payload = valid_payload(request)
    payload["overall"] = 4 + (judge_index % 2)
    for dim_index, dimension in enumerate(JUDGE_DIMENSIONS):
        score = 4 + ((judge_index + dim_index) % 2)
        payload["dimension_scores"][dimension] = {
            "score": score,
            "rationale": f"{dimension} graded {score} against the rubric anchors",
        }
    return payload


def abstaining_payload(request) -> dict:
    """Valid qualifying-score payload that nonetheless abstains."""
    payload = valid_payload(request)
    payload["abstained"] = True
    payload["abstention_reason"] = "required source excerpt unreadable"
    return payload


def passing_gate():
    return {
        "passed": True,
        "failures": [],
        "producer_fingerprint": PRODUCER_IDENTITY,
    }


def failing_gate():
    return {
        "passed": False,
        "failures": [
            {
                "code": "metric_conflict",
                "severity": "high",
                "root_category": "arithmetic",
                "path": "facts.metrics.revenue.value",
                "evidence": "revenue value contradicts excerpt",
            }
        ],
        "producer_fingerprint": PRODUCER_IDENTITY,
    }


def contradictory_gate():
    """Affirmative pass flag contradicted by a well-formed failure."""
    return {
        "passed": True,
        "failures": [failing_gate()["failures"][0]],
        "producer_fingerprint": PRODUCER_IDENTITY,
    }


def malformed_gate_variants():
    """Gate payloads whose shape must fail closed WITHOUT raising.

    Every variant carries the run identity so aggregation grades it as a
    failing hard gate rather than refusing it. Shapes that cannot carry an
    identity at all (``object()``, ``None``) raise as cross-run mixups in
    ``test_unidentifiable_gate_report_raises_as_cross_run_mixup``.
    """
    return [
        {"passed": "yes", "failures": [], "producer_fingerprint": PRODUCER_IDENTITY},
        {"passed": 1, "failures": [], "producer_fingerprint": PRODUCER_IDENTITY},
        {"passed": None, "failures": [], "producer_fingerprint": PRODUCER_IDENTITY},
        {"failures": [], "producer_fingerprint": PRODUCER_IDENTITY},
        {
            "passed": False,
            "failures": "metric_conflict: revenue",
            "producer_fingerprint": PRODUCER_IDENTITY,
        },
        {
            "passed": False,
            "failures": {"code": "metric_conflict"},
            "producer_fingerprint": PRODUCER_IDENTITY,
        },
        {
            "passed": False,
            "failures": [["nested"]],
            "producer_fingerprint": PRODUCER_IDENTITY,
        },
        {
            "passed": False,
            "failures": [{"severity": "high"}],
            "producer_fingerprint": PRODUCER_IDENTITY,
        },
    ]


def floored_payload(request, dimension) -> dict:
    """Qualifying payload with exactly one dimension scored 3 by this judge."""
    payload = valid_payload(request)
    payload["dimension_scores"][dimension] = {
        "score": 3,
        "rationale": f"{dimension} graded 3 against the rubric anchors",
    }
    return payload


def floored_results(requests, dimension):
    """One result per request; every judge scores ``dimension`` below 4."""
    return [
        parse_judge_result(request, floored_payload(request, dimension))
        for request in requests
    ]


def edge_floor_payload(request, dimension, score) -> dict:
    """Qualifying payload with one dimension scored ``score`` by this judge."""
    payload = valid_payload(request)
    payload["dimension_scores"][dimension] = {
        "score": score,
        "rationale": f"{dimension} graded {score} against the rubric anchors",
    }
    return payload


class RequestConstructionTests(unittest.TestCase):
    def test_uses_strict_v4_prompt_and_schema_contract(self):
        self.assertEqual(PROMPT_VERSION, "company_judge_prompt_v4")
        self.assertEqual(SCHEMA_VERSION, "company_blind_judge_v4")

    def test_default_finalized_fixture_carries_exact_empty_v7_relationships(self):
        finalized = finalized_analysis()
        for material in (finalized.facts, finalized.analysis):
            self.assertEqual(material["relationship_facts"], {})
            self.assertEqual(material["material_relationships"], [])
            self.assertEqual(material["relationship_reconciliations"], [])
            self.assertEqual(
                material["counter_thesis"],
                "Pricing power erodes as customer budgets compress.",
            )
            self.assertEqual(
                set(material["materiality_assessment"]),
                {
                    "forward_guidance",
                    "reported_variance_driver",
                    "margin_economics",
                    "capital_commitment_duration",
                },
            )
        self.assertEqual(
            set(finalized.analysis["risks"][0]),
            {
                "sourced_observation",
                "inference",
                "epistemic_state",
                "uncertainty",
                "likelihood",
                "impact",
                "mitigation",
                "evidence",
            },
        )
        self.assertEqual(
            set(finalized.analysis["catalysts"][0]),
            {
                "trigger",
                "expected_outcome",
                "horizon",
                "epistemic_state",
                "uncertainty",
                "evidence",
            },
        )

    def test_populated_relationship_contract_reaches_every_judge_unchanged(self):
        finalized = populated_relationship_finalized_analysis()
        requests = build_blind_judge_requests(
            producer_case(), evaluator_case(), finalized, SALT_A
        )
        for field in (
            "relationship_facts",
            "material_relationships",
            "relationship_reconciliations",
        ):
            self.assertTrue(finalized.facts[field])
            self.assertEqual(finalized.analysis[field], finalized.facts[field])
        packet_marker = f'<case_packet as_of="{AS_OF.isoformat()}">\n'
        packet_decoder = json.JSONDecoder()
        for request in requests:
            dispatch = request.packet()
            prompt = dispatch["prompt"]
            self.assertEqual(prompt.count(packet_marker), 1)
            packet_source = prompt.partition(packet_marker)[2]
            packet, packet_end = packet_decoder.raw_decode(packet_source)
            self.assertEqual(packet_source[packet_end:], "\n</case_packet>")
            material = packet["untrusted_material"]
            for field in (
                "relationship_facts",
                "material_relationships",
                "relationship_reconciliations",
            ):
                self.assertEqual(material[field], finalized.facts[field])
                self.assertEqual(
                    material["producer_facts"][field], finalized.facts[field]
                )
                self.assertEqual(
                    material["producer_analysis"][field],
                    finalized.analysis[field],
                )

    def test_exactly_three_unique_roles(self):
        requests = build_requests()
        self.assertEqual(len(requests), 3)
        self.assertEqual({request.role for request in requests}, set(JUDGE_ROLES))
        self.assertEqual(len(set(JUDGE_ROLES)), 3)

    def test_deterministic_per_salt(self):
        first = build_requests(SALT_A)
        second = build_requests(SALT_A)
        self.assertEqual(
            [request.role for request in first], [request.role for request in second]
        )
        self.assertEqual(
            [request.token for request in first], [request.token for request in second]
        )
        self.assertEqual(
            [request.prompt for request in first],
            [request.prompt for request in second],
        )
        self.assertEqual(
            [request.fingerprint for request in first],
            [request.fingerprint for request in second],
        )

    def test_salt_changes_tokens_fingerprints_and_order(self):
        alpha = build_requests(SALT_A)
        alpha_order = [request.role for request in alpha]
        alpha_tokens = [request.token for request in alpha]
        alpha_prints = [request.fingerprint for request in alpha]
        # Token equality across distinct salts would require an HMAC-SHA256
        # collision on distinct inputs, so token divergence is guaranteed;
        # permutation equality has ~1/6 odds per salt, so order divergence is
        # asserted once at least one of many candidates separates the orders.
        order_separated = None
        for index in range(12):
            candidate = build_requests(f"run-salt-{index}")
            self.assertNotEqual([request.token for request in candidate], alpha_tokens)
            self.assertNotEqual(
                [request.fingerprint for request in candidate], alpha_prints
            )
            candidate_order = [request.role for request in candidate]
            if candidate_order != alpha_order and order_separated is None:
                order_separated = candidate_order
        self.assertIsNotNone(order_separated)

    def test_salt_forms_deterministic_distinct_and_opaque(self):
        # ``_salt_bytes`` treats a str as UTF-8 text and bytes verbatim, so a
        # hex string and its decoded bytes are intentionally different salts.
        salt_hex = hashlib.sha256(SALT_B.encode("utf-8")).hexdigest()
        decoded = bytes.fromhex(salt_hex)
        str_first = build_requests(salt_hex)
        str_second = build_requests(salt_hex)
        bytes_first = build_requests(decoded)
        bytes_second = build_requests(decoded)
        self.assertEqual(
            [request.token for request in str_first],
            [request.token for request in str_second],
        )
        self.assertEqual(
            [request.token for request in bytes_first],
            [request.token for request in bytes_second],
        )
        self.assertNotEqual(
            [request.token for request in str_first],
            [request.token for request in bytes_first],
        )
        tokens = [request.token for request in str_first]
        self.assertEqual(len(tokens), len(set(tokens)))  # unique per role
        for token in tokens:
            self.assertEqual(len(token), 32)
            int(token, 16)  # opaque hex digest

    def test_prompt_and_packet_blindness(self):
        requests = build_requests()
        forbidden_markers = [
            "model",
            "gpt",
            "claude",
            "llm",
            "provider",
            "provenance",
            "extraction",
            "iteration",
            "attempt",
            "champion",
            "threshold",
            "median",
            "later_outcomes",
            "forbidden_hindsight",
            "blind_salt",
            SALT_A,
        ]
        for request in requests:
            packet = request.packet()
            packet_json = json.dumps(packet, sort_keys=True)
            prompt = request.prompt
            combined = prompt + packet_json
            # Sibling roles never appear; the judge sees only its own role.
            for sibling in JUDGE_ROLES:
                if sibling != request.role:
                    self.assertNotIn(sibling, combined)
            for marker in forbidden_markers:
                self.assertNotIn(marker.lower(), combined.lower(), f"leaked {marker!r}")
            # Packet carries exactly the dispatch contract.
            self.assertEqual(
                set(packet),
                {"prompt", "schema_name", "strict", "schema"},
            )
            self.assertIn("evaluation_rubric", prompt)
            self.assertIn("strongest_counter_thesis", prompt)
            self.assertIn(request.role, prompt)
            self.assertIn(request.token, prompt)
            self.assertIn(PROMPT_VERSION, prompt)
            self.assertIn(request.response_binding, prompt)
            self.assertIn("response_binding=", prompt)
            # Settled v4 invariant: the prompt carries the response binding
            # only — never the canonical request fingerprint, never the raw
            # producer identity literal.
            self.assertNotIn(request.fingerprint, prompt)
            self.assertNotIn(PRODUCER_IDENTITY, prompt)
            self.assertNotIn("later_outcomes", packet_json)
            self.assertNotIn("forbidden_hindsight", packet_json)
        # Structured forbidden claims never reach the prompt or the packet:
        # neither their ids/aliases nor the claim value appear anywhere.
        for request in requests:
            combined = request.prompt + json.dumps(request.packet(), sort_keys=True)
            self.assertNotIn("capex_q1", combined)
            self.assertNotIn("period_aliases", combined)
            self.assertNotIn("metric_aliases", combined)

    def test_judges_receive_epistemic_risk_and_catalyst_contract(self):
        for request in build_requests():
            prompt = request.prompt
            for field in (
                "sourced_observation",
                "inference",
                "epistemic_state",
                "uncertainty",
                "trigger",
                "expected_outcome",
                "horizon",
                "evidence",
                "relationship_reconciliations",
                "counter_thesis",
                "materiality_assessment",
            ):
                self.assertIn(f'"{field}"', prompt)
            self.assertIn("Demand softness could pressure future margins.", prompt)
            self.assertIn("Revenue lands within the guided range", prompt)
            self.assertIn("Pricing power erodes as customer budgets compress.", prompt)
            for topic in (
                "forward_guidance",
                "reported_variance_driver",
                "margin_economics",
                "capital_commitment_duration",
            ):
                self.assertIn(f'"{topic}"', prompt)
            self.assertNotIn('"risk":', prompt)
            self.assertNotIn('"catalyst":', prompt)

    def test_packet_is_deep_copied_per_call(self):
        request = build_requests()[0]
        first = request.packet()
        first["schema"]["properties"]["overall"] = None
        second = request.packet()
        self.assertIsNotNone(second["schema"]["properties"]["overall"])

    def test_missing_or_malformed_producer_fingerprint_rejected(self):
        for broken_identity in ("", "   ", "not-hex", "F" * 64, None, 7):
            with self.subTest(identity=repr(broken_identity)[:16]):
                broken = producer_case(fingerprint=broken_identity)
                with self.assertRaises(ValueError):
                    build_blind_judge_requests(
                        broken, evaluator_case(), finalized_analysis(), SALT_A
                    )

    def test_request_schema_is_recursively_frozen(self):
        for request in build_requests():
            self.assertEqual(request.schema_name, SCHEMA_NAME)
            self.assertIsInstance(request.schema, MappingProxyType)
            self.assertIsInstance(request.schema["properties"], MappingProxyType)
            self.assertIsInstance(
                request.schema["properties"]["dimension_scores"]["properties"],
                MappingProxyType,
            )
            required = request.schema["properties"]["dimension_scores"]["required"]
            self.assertIsInstance(required, tuple)
            self.assertEqual(list(required), list(JUDGE_DIMENSIONS))
            with self.assertRaises(TypeError):
                request.schema["strict"] = True
            with self.assertRaises((TypeError, AttributeError)):
                request.schema["properties"]["dimension_scores"]["required"].append(
                    "extra"
                )

    def test_packet_and_dict_are_independent_plain_copies(self):
        for request in build_requests():
            prompt_before = request.prompt
            fingerprint_before = request.fingerprint
            binding_before = request.response_binding
            packet = request.packet()
            as_dict = request.to_dict()
            self.assertEqual(set(packet), {"prompt", "schema_name", "strict", "schema"})
            self.assertEqual(
                set(as_dict),
                set(packet)
                | {
                    "role",
                    "token",
                    "prompt_version",
                    "response_binding",
                    "producer_fingerprint",
                },
            )
            # Materialized payloads are plain JSON-native structures.
            self.assertNotIsInstance(packet["schema"], MappingProxyType)
            self.assertNotIsInstance(as_dict["schema"], MappingProxyType)
            packet["schema"]["properties"]["overall"]["maximum"] = 99
            packet["schema"]["required"].append("injected")
            as_dict["schema"]["properties"]["overall"]["type"] = "number"
            as_dict["prompt"] = "tampered"
            as_dict["response_binding"] = "forged"
            # Mutating the copies leaves the stored request untouched.
            fresh_packet = request.packet()
            fresh_dict = request.to_dict()
            for materialized in (fresh_packet, fresh_dict):
                self.assertEqual(
                    materialized["schema"]["properties"]["overall"]["maximum"],
                    SCORE_MAXIMUM,
                )
                self.assertEqual(
                    materialized["schema"]["properties"]["overall"]["type"],
                    "number",
                )
                self.assertNotIn("injected", materialized["schema"]["required"])
                self.assertIn("token", materialized["schema"]["required"])
            self.assertEqual(fresh_packet, request.packet())
            self.assertEqual(request.prompt, prompt_before)
            self.assertEqual(request.fingerprint, fingerprint_before)
            self.assertEqual(request.response_binding, binding_before)

    def test_dispatch_materialization_never_aliases_stored_request(self):
        baseline = {
            request.role: (
                request.token,
                request.prompt_version,
                request.fingerprint,
                request.response_binding,
                json.dumps(request.packet(), sort_keys=True),
            )
            for request in build_requests()
        }
        requests = build_requests()
        for request in requests:
            packet = request.packet()
            packet["prompt"] = "forged prompt"
            packet["schema_name"] = "other"
            packet["strict"] = False
            packet["schema"]["required"] = ["role"]
            packet["schema"]["properties"]["dimension_scores"] = None
            as_dict = request.to_dict()
            as_dict["response_binding"] = "forged binding"
            as_dict["producer_fingerprint"] = OTHER_IDENTITY
            as_dict["schema"]["required"] = ["role"]
            as_dict["schema"]["properties"] = {}
            token, version, print_, binding, packet_json = baseline[request.role]
            self.assertEqual(request.token, token)
            self.assertEqual(request.prompt_version, version)
            self.assertEqual(request.fingerprint, print_)
            self.assertEqual(request.response_binding, binding)
            self.assertEqual(request.producer_fingerprint, PRODUCER_IDENTITY)
            self.assertEqual(json.dumps(request.packet(), sort_keys=True), packet_json)
            fresh = parse_judge_result(request, valid_payload(request))
            self.assertEqual(fresh.token, request.token)
            self.assertEqual(fresh.response_binding, request.response_binding)

    def test_prompt_bytes_stable_under_payload_mutation(self):
        requests = build_requests()
        prompts_before = [request.prompt for request in requests]
        fingerprints_before = [request.fingerprint for request in requests]
        for request in requests:
            payload = request.packet()
            payload["schema"]["properties"]["role"]["type"] = "integer"
            payload["schema"]["properties"].pop("token")
        self.assertEqual([request.prompt for request in requests], prompts_before)
        self.assertEqual(
            [request.fingerprint for request in requests], fingerprints_before
        )


class ParseJudgeResultTests(unittest.TestCase):
    def setUp(self):
        self.requests = build_requests()
        self.request = self.requests[0]

    def test_accepts_exact_valid_result_from_string_and_mapping(self):
        payload = valid_payload(self.request)
        from_string = parse_judge_result(self.request, json.dumps(payload))
        from_mapping = parse_judge_result(self.request, payload)
        self.assertEqual(from_string.overall, 5)
        self.assertEqual(from_mapping.overall, 5)
        self.assertEqual(len(from_string.scores), len(JUDGE_DIMENSIONS))
        self.assertFalse(from_string.abstained)
        self.assertFalse(from_string.severe_regression)
        self.assertEqual(
            from_string.concrete_defects,
            ("summary omits the guided revenue range",),
        )
        # Every dimension rationale is nonblank text, even at the top score.
        for score in from_string.scores:
            self.assertTrue(score.rationale and score.rationale.strip())

    def test_result_is_bound_to_request_identity(self):
        result = parse_judge_result(self.request, valid_payload(self.request))
        self.assertEqual(result.role, self.request.role)
        self.assertEqual(result.token, self.request.token)
        self.assertEqual(result.response_binding, self.request.response_binding)
        self.assertEqual(result.prompt_version, PROMPT_VERSION)
        self.assertTrue(
            all(score.dimension in JUDGE_DIMENSIONS for score in result.scores)
        )

    def _assert_rejected(self, mutate):
        payload = valid_payload(self.request)
        mutate(payload)
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, payload)

    def test_rejects_missing_key(self):
        self._assert_rejected(lambda payload: payload.pop("abstention_reason"))

    def test_rejects_extra_key(self):
        self._assert_rejected(lambda payload: payload.update({"notes": "extra"}))

    def test_rejects_bool_overall(self):
        self._assert_rejected(lambda payload: payload.update({"overall": True}))

    def test_accepts_tenth_step_scores(self):
        """The old integer schema rejected every decimal here; the
        fractional contract accepts exact tenth-step values verbatim."""
        payload = valid_payload(self.request)
        payload["overall"] = 4.5
        for dimension in JUDGE_DIMENSIONS:
            payload["dimension_scores"][dimension] = {
                "score": 4.8,
                "rationale": f"{dimension} graded 4.8 against the anchors",
            }
        from_string = parse_judge_result(self.request, json.dumps(payload))
        self.assertEqual(from_string.overall, 4.5)
        self.assertEqual(from_string.score_for(JUDGE_DIMENSIONS[3]), 4.8)
        from_mapping = parse_judge_result(self.request, payload)
        self.assertEqual(from_mapping.overall, 4.5)
        self.assertTrue(all(score.score == 4.8 for score in from_mapping.scores))

    def test_rejects_out_of_range_overall(self):
        self._assert_rejected(
            lambda payload: payload.update({"overall": SCORE_MAXIMUM + 1})
        )
        self._assert_rejected(
            lambda payload: payload.update({"overall": SCORE_MINIMUM - 1})
        )

    def test_rejects_bool_dimension_score(self):
        def mutate(payload):
            payload["dimension_scores"][JUDGE_DIMENSIONS[0]]["score"] = False

        self._assert_rejected(mutate)

    def test_rejects_out_of_range_dimension_score(self):
        def make(score):
            def mutate(payload):
                payload["dimension_scores"][JUDGE_DIMENSIONS[1]]["score"] = score

            return mutate

        self._assert_rejected(make(SCORE_MAXIMUM + 1))
        self._assert_rejected(make(SCORE_MINIMUM - 1))

    def test_rejects_wrong_role(self):
        other_role = self.requests[1].role
        self._assert_rejected(lambda payload: payload.update({"role": other_role}))

    def test_rejects_wrong_token(self):
        self._assert_rejected(lambda payload: payload.update({"token": "f" * 32}))

    def test_rejects_wrong_prompt_version(self):
        self._assert_rejected(
            lambda payload: payload.update(
                {"prompt_version": "company_judge_prompt_v0"}
            )
        )

    def test_rejects_wrong_response_binding(self):
        # The sibling request's binding is well-formed but bound to another
        # role/token: the parser must refuse it for this request.
        self._assert_rejected(
            lambda payload: payload.update(
                {"response_binding": self.requests[1].response_binding}
            )
        )

    def test_rejects_binding_from_another_run_salt(self):
        other_run = build_requests(SALT_B)
        foreign = next(
            request for request in other_run if request.role == self.request.role
        )
        self._assert_rejected(
            lambda payload: payload.update(
                {"response_binding": foreign.response_binding}
            )
        )

    def test_rejects_malformed_response_bindings(self):
        malformed = [
            "not-hex",
            "A" * 64,  # uppercase hex is not a canonical binding
            ("a" * 63),  # truncated digest
            ("a" * 64 + "0"),  # overlong digest
            PRODUCER_IDENTITY,  # well-formed digest of the wrong domain
            "",
            "   ",
            None,
            7,
        ]
        for value in malformed:
            with self.subTest(binding=repr(value)[:24]):
                payload = valid_payload(self.request)
                payload["response_binding"] = value
                with self.assertRaises(ValueError):
                    parse_judge_result(self.request, payload)

    def test_result_carries_response_binding_not_request_fingerprint(self):
        result = parse_judge_result(self.request, valid_payload(self.request))
        self.assertEqual(result.response_binding, self.request.response_binding)
        # The canonical request fingerprint never travels on results and is
        # never echoed by the judge inside its own response contract.
        self.assertNotEqual(result.response_binding, self.request.fingerprint)
        self.assertFalse(
            any("fingerprint" in name for name in type(result).__dataclass_fields__)
        )

    def test_rejects_sub4_dimension_without_rationale(self):
        def mutate(payload):
            payload["dimension_scores"][JUDGE_DIMENSIONS[2]] = {
                "score": 3,
                "rationale": None,
            }

        self._assert_rejected(mutate)

    def test_rejects_null_rationale_even_at_top_score(self):
        payload = valid_payload(self.request)
        payload["dimension_scores"][JUDGE_DIMENSIONS[0]] = {
            "score": 5,
            "rationale": None,
        }
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, payload)

    def test_rejects_blank_rationale_even_at_top_score(self):
        payload = valid_payload(self.request)
        payload["dimension_scores"][JUDGE_DIMENSIONS[0]] = {
            "score": 5,
            "rationale": "   ",
        }
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, payload)

    def test_rejects_sub4_overall_without_concrete_defect(self):
        payload = valid_payload(self.request)
        payload["overall"] = 3
        payload["concrete_defects"] = []
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, payload)

    def test_rejects_blank_defect_entry(self):
        self._assert_rejected(
            lambda payload: payload.update({"concrete_defects": ["   "]})
        )

    def test_rejects_nonstring_defect_entry(self):
        self._assert_rejected(lambda payload: payload.update({"concrete_defects": [7]}))

    def test_severe_regression_defect_evidence_is_mandatory(self):
        # The reason is advisory; the defect evidence is the load-bearing
        # requirement: a severe regression without any located defect is
        # rejected even when a reason is supplied, and a missing reason
        # alone stays parseable.
        def without_reason(payload):
            payload["severe_regression"] = True
            payload["severe_regression_reason"] = None

        payload = valid_payload(self.request)
        without_reason(payload)
        accepted = parse_judge_result(self.request, payload)
        self.assertTrue(accepted.severe_regression)
        self.assertIsNone(accepted.severe_regression_reason)

        def without_defects(payload):
            payload["severe_regression"] = True
            payload["severe_regression_reason"] = "materially misleading summary"
            payload["concrete_defects"] = []

        self._assert_rejected(without_defects)

    def test_accepts_severe_regression_with_reason_and_defect(self):
        payload = valid_payload(self.request)
        payload["severe_regression"] = True
        payload["severe_regression_reason"] = "materially misleading summary"
        result = parse_judge_result(self.request, payload)
        self.assertTrue(result.severe_regression)
        self.assertEqual(
            result.severe_regression_reason, "materially misleading summary"
        )

    def test_rejects_abstention_without_reason(self):
        def mutate(payload):
            payload["abstained"] = True
            payload["abstention_reason"] = None

        self._assert_rejected(mutate)

    def test_accepts_abstention_with_reason(self):
        payload = valid_payload(self.request)
        payload["abstained"] = True
        payload["abstention_reason"] = "source excerpt missing"
        result = parse_judge_result(self.request, payload)
        self.assertTrue(result.abstained)

    def test_rejects_malformed_json_and_nonobject(self):
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, "{not json")
        # Non-object raw JSON — arrays, scalars, nulls — must fail closed.
        for raw in (
            json.dumps([valid_payload(self.request)]),
            json.dumps("role"),
            json.dumps(7),
            "null",
            "true",
        ):
            with self.subTest(raw=raw[:24]):
                with self.assertRaises(ValueError):
                    parse_judge_result(self.request, raw)

    def test_rejects_extra_or_missing_dimension(self):
        def drop(payload):
            del payload["dimension_scores"][JUDGE_DIMENSIONS[0]]

        self._assert_rejected(drop)

        def add(payload):
            payload["dimension_scores"]["made_up"] = {
                "score": 4,
                "rationale": "unexpected dimension",
            }

        self._assert_rejected(add)

    def test_rejects_malformed_dimension_entry(self):
        def mutate(payload):
            payload["dimension_scores"][JUDGE_DIMENSIONS[3]] = {"score": 4}

        self._assert_rejected(mutate)


class AggregateJudgePanelTests(unittest.TestCase):
    def setUp(self):
        self.requests = build_requests()
        self.results = qualifying_results(self.requests)

    def _criterion(self, report, name):
        return next(c for c in report.criteria if c.criterion == name)

    def test_passes_exact_qualifying_panel(self):
        report = aggregate_judge_panel(self.requests, self.results, passing_gate())
        self.assertTrue(report.passed)
        criteria = {c.criterion: c.passed for c in report.criteria}
        self.assertTrue(criteria)
        self.assertTrue(all(criteria.values()))
        self.assertEqual(
            set(criteria),
            {
                "hard_gates_pass",
                "panel_overall_median",
                "all_dimension_medians",
                "factual_fidelity_median",
                "materiality_median",
                "counter_thesis_strength_median",
                "no_severe_regression",
                "no_abstentions",
                "core_dimension_judge_floor",
            },
        )
        self.assertEqual(report.severe_regression_roles, ())
        self.assertEqual(report.abstained_roles, ())
        self.assertEqual(report.gate_failures, ())
        self.assertEqual(report.producer_fingerprint, PRODUCER_IDENTITY)
        self.assertEqual(report.to_dict()["producer_fingerprint"], PRODUCER_IDENTITY)

    def test_computes_medians_and_minima(self):
        results = [
            parse_judge_result(request, varied_payload(request, index))
            for index, request in enumerate(self.requests)
        ]
        report = aggregate_judge_panel(self.requests, results, passing_gate())
        self.assertIsInstance(report.overall_median, float)
        overalls = sorted(result.overall for result in results)
        self.assertEqual(report.overall_median, float(overalls[1]))
        self.assertEqual(report.overall_median, 4.0)
        for dimension in JUDGE_DIMENSIONS:
            values = sorted(result.score_for(dimension) for result in results)
            self.assertEqual(report.dimension_medians[dimension], float(values[1]))
            self.assertEqual(report.dimension_minima[dimension], values[0])
        # Every dimension sees a 4/5 mix across judges: minima are all 4 and
        # medians land on 4.0 or 5.0 depending on parity alignment.
        self.assertEqual(set(report.dimension_minima.values()), {4})
        self.assertLessEqual(set(report.dimension_medians.values()), {4.0, 5.0})

    def _report_with_overalls(self, overalls):
        results = []
        for request, overall in zip(self.requests, overalls, strict=True):
            payload = valid_payload(request)
            payload["overall"] = overall
            results.append(parse_judge_result(request, payload))
        return aggregate_judge_panel(self.requests, results, passing_gate())

    def test_overall_median_threshold_boundary(self):
        # Medians of three integers land on .0/.33/.67 steps: 4.0 sits below
        # OVERALL_MEDIAN_MINIMUM, 5.0 clears it.
        passing = self._report_with_overalls((4, 5, 5))
        self.assertEqual(passing.overall_median, 5.0)
        self.assertGreaterEqual(passing.overall_median, OVERALL_MEDIAN_MINIMUM)
        self.assertTrue(passing.passed)
        failing = self._report_with_overalls((5, 4, 4))
        self.assertEqual(failing.overall_median, 4.0)
        self.assertLess(failing.overall_median, OVERALL_MEDIAN_MINIMUM)
        self.assertFalse(self._criterion(failing, "panel_overall_median").passed)

    def _dimension_panel(self, name, scores):
        """Aggregate valid request-bound results with one dimension rescored."""
        results = []
        for request, score in zip(self.requests, scores, strict=True):
            payload = valid_payload(request)
            payload["dimension_scores"][name] = {
                "score": score,
                "rationale": f"{name} graded {score} against the rubric anchors",
            }
            results.append(parse_judge_result(request, payload))
        return aggregate_judge_panel(self.requests, results, passing_gate())

    def test_generic_dimension_floor_at_and_below_minimum(self):
        name = "synthesis_decision_usefulness"
        at_minimum = self._dimension_panel(name, (4, 4, 4))
        self.assertEqual(at_minimum.dimension_medians[name], DIMENSION_MEDIAN_MINIMUM)
        self.assertTrue(self._criterion(at_minimum, "all_dimension_medians").passed)
        self.assertTrue(at_minimum.passed)
        below = self._dimension_panel(name, (3, 3, 3))
        self.assertEqual(below.dimension_medians[name], 3.0)
        self.assertLess(below.dimension_medians[name], DIMENSION_MEDIAN_MINIMUM)
        self.assertFalse(self._criterion(below, "all_dimension_medians").passed)
        self.assertFalse(below.passed)

    def test_named_dimension_median_boundaries(self):
        # Integer medians over three judges can only be 3, 4, or 5, so the
        # fractional floors (>4) are cleared only by a median of 5 and
        # missed by any median <= 4; the generic floor of 4.0 is probed
        # exactly in ``test_generic_dimension_floor_at_and_below_minimum``.
        cases = [
            ("factual_fidelity", FACTUAL_FIDELITY_MEDIAN_MINIMUM, (5, 5, 4), (5, 4, 4)),
            ("materiality", MATERIALITY_MEDIAN_MINIMUM, (5, 5, 4), (5, 4, 4)),
            (
                "counter_thesis_strength",
                COUNTER_THESIS_STRENGTH_MEDIAN_MINIMUM,
                (5, 5, 4),
                (5, 4, 4),
            ),
        ]
        for name, threshold, passing_triple, failing_triple in cases:
            with self.subTest(dimension=name):
                passing = self._dimension_panel(name, passing_triple)
                median = passing.dimension_medians[name]
                self.assertGreaterEqual(median, threshold)
                self.assertTrue(self._criterion(passing, f"{name}_median").passed)
                self.assertTrue(passing.passed)
                failing = self._dimension_panel(name, failing_triple)
                median = failing.dimension_medians[name]
                self.assertLess(median, threshold)
                self.assertFalse(self._criterion(failing, f"{name}_median").passed)
                self.assertFalse(failing.passed)

    def test_hard_gate_failure_fails_panel(self):
        report = aggregate_judge_panel(self.requests, self.results, failing_gate())
        self.assertFalse(report.passed)
        self.assertFalse(self._criterion(report, "hard_gates_pass").passed)
        self.assertEqual(len(report.gate_failures), 1)
        failure = report.gate_failures[0]
        self.assertEqual(failure.code, "metric_conflict")
        self.assertEqual(failure.severity, "high")
        self.assertEqual(failure.root_category, "arithmetic")
        self.assertEqual(failure.path, "facts.metrics.revenue.value")
        self.assertIn("revenue", failure.evidence)

    def test_hard_gate_report_without_identity_is_rejected(self):
        # An absent or foreign hard-gate report identity is a cross-run
        # mixup, not a failed gate: aggregation must refuse the report
        # outright rather than silently grading it as failing gates.
        with self.assertRaises(ValueError):
            aggregate_judge_panel(self.requests, self.results, {})
        with self.assertRaises(ValueError):
            aggregate_judge_panel(
                self.requests,
                self.results,
                {
                    "passed": True,
                    "failures": [],
                    "producer_fingerprint": OTHER_IDENTITY,
                },
            )

    def test_panel_report_serializes_producer_identity(self):
        report = aggregate_judge_panel(self.requests, self.results, passing_gate())
        self.assertEqual(report.producer_fingerprint, PRODUCER_IDENTITY)
        rendered = report.to_dict()
        self.assertEqual(rendered["producer_fingerprint"], PRODUCER_IDENTITY)

    def test_requests_disagreeing_on_identity_are_rejected(self):
        # One request from a different producer run (same role, different
        # token/fingerprint/producer identity) makes the panel refuse to
        # aggregate: requests must agree on one valid run identity.
        other = build_requests(SALT_B)
        swapped_role = self.requests[0].role
        foreign_request = next(r for r in other if r.role == swapped_role)
        hybrid = [foreign_request, *self.requests[1:]]
        with self.assertRaises(ValueError):
            aggregate_judge_panel(hybrid, self.results, passing_gate())

    def test_request_producer_identity_attribute_must_be_valid(self):
        # The builder validates the identity it derives from the producer
        # case ``fingerprint``; a stand-in carrying a non-hex or blank
        # identity must fail closed before any request exists.
        for broken_identity in ("not-hex", "", None):
            with self.subTest(identity=repr(broken_identity)):
                broken = producer_case(fingerprint=broken_identity)
                with self.assertRaises(ValueError):
                    build_blind_judge_requests(
                        broken, evaluator_case(), finalized_analysis(), SALT_A
                    )

    def test_directly_constructed_request_validates_identity(self):
        schema = {"type": "object"}
        with self.assertRaises(ValueError):
            BlindJudgeRequest(
                role=JUDGE_ROLES[0],
                token="a" * 32,
                prompt_version=PROMPT_VERSION,
                schema_name=SCHEMA_NAME,
                strict=True,
                schema=schema,
                prompt="p",
                fingerprint="b" * 64,
                response_binding="c" * 64,
                producer_fingerprint="short",
            )
        with self.assertRaises(ValueError):
            BlindJudgeRequest(
                role=JUDGE_ROLES[0],
                token="a" * 32,
                prompt_version=PROMPT_VERSION,
                schema_name=SCHEMA_NAME,
                strict=True,
                schema=schema,
                prompt="p",
                fingerprint="b" * 64,
                response_binding="not-hex",
                producer_fingerprint=PRODUCER_IDENTITY,
            )

    def test_request_content_fingerprint_binds_the_producer(self):
        requests = build_requests()
        for request in requests:
            recomputed = canonical_fingerprint(
                {
                    "kind": "blind_judge_request",
                    "schema_version": SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "role": request.role,
                    "token": request.token,
                    "schema_name": SCHEMA_NAME,
                    "producer_fingerprint": request.producer_fingerprint,
                    "schema": dict(request.schema),
                    "prompt": request.prompt,
                }
            )
            self.assertEqual(request.fingerprint, recomputed)
            # The producer identity binds the request through the digest and
            # the salt-bound binding, never as a literal prompt echo.
            self.assertNotIn(PRODUCER_IDENTITY, request.prompt)

    def test_response_binding_is_rendered_distinct_and_opaque(self):
        requests = build_requests()
        bindings = [request.response_binding for request in requests]
        self.assertEqual(len(set(bindings)), len(bindings))
        for request in requests:
            # Well-formed digest shape; opaque (not the token or identity).
            self.assertRegex(request.response_binding, r"^[0-9a-f]{64}$")
            self.assertNotEqual(request.response_binding, request.token)
            self.assertNotEqual(request.response_binding, PRODUCER_IDENTITY)
            # Rendered verbatim into the prompt exactly once, and the
            # executor-visible prompt states the same value as the attribute.
            self.assertIn(request.response_binding, request.prompt)
            self.assertEqual(request.prompt.count(request.response_binding), 1)
            self.assertEqual(stated_response_binding(request), request.response_binding)
            # The old ambiguous echo never re-enters the response contract.
            self.assertNotIn("fingerprint=", request.prompt)
            self.assertNotIn("fingerprint", request.to_dict())

    def test_binding_moves_with_salt_and_packet_but_fingerprint_stays_canonical(self):
        alpha = build_requests(SALT_A)
        beta = build_requests(SALT_B)
        for fresh in beta:
            same_role = next(request for request in alpha if request.role == fresh.role)
            self.assertNotEqual(fresh.response_binding, same_role.response_binding)
        # Same inputs reproduce the identical binding byte-for-byte.
        replayed = build_requests(SALT_A)
        self.assertEqual(
            [request.response_binding for request in replayed],
            [request.response_binding for request in alpha],
        )
        for request in alpha + tuple(replayed):
            # The binding covers the rendered packet: a different case packet
            # under any salt yields a distinct value (cross-run check above);
            # the canonical fingerprint still digests the exact rendered
            # request including its embedded binding.
            self.assertIn(request.response_binding, request.prompt)
            recomputed = canonical_fingerprint(
                {
                    "kind": "blind_judge_request",
                    "schema_version": SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "role": request.role,
                    "token": request.token,
                    "schema_name": SCHEMA_NAME,
                    "producer_fingerprint": request.producer_fingerprint,
                    "schema": dict(request.schema),
                    "prompt": request.prompt,
                }
            )
            self.assertEqual(request.fingerprint, recomputed)

    def test_prompt_or_schema_mutation_moves_fingerprint_but_not_rendered_binding(self):
        # The binding is computed BEFORE rendering, from the packet and the
        # prompt template. Mutating a request's own post-render content —
        # prompt text or response schema — therefore moves the canonical
        # request fingerprint while the already-rendered binding field stays
        # byte-identical. The identifiers are independent by construction:
        # the fingerprint detects request tampering that a binding echo
        # alone cannot see, so artifacts must persist and validate both.
        for original in build_requests():
            mutated_schema = dict(original.schema)
            mutated_schema["properties"] = {
                **mutated_schema["properties"],
                "overall": {"type": "integer", "minimum": 1, "maximum": 4},
            }
            tampered_prompt = original.prompt + "\nINJECTED INSTRUCTION"
            mutations = (
                {"schema": mutated_schema, "prompt": original.prompt},
                {"schema": original.schema, "prompt": tampered_prompt},
            )
            for mutation in mutations:
                # Carry the STALE digest and the untouched rendered binding:
                # an edited request cannot mint either value honestly.
                tampered = BlindJudgeRequest.with_frozen_schema(
                    role=original.role,
                    token=original.token,
                    prompt_version=original.prompt_version,
                    schema_name=original.schema_name,
                    strict=original.strict,
                    fingerprint=original.fingerprint,
                    response_binding=original.response_binding,
                    producer_fingerprint=original.producer_fingerprint,
                    **mutation,
                )
                self.assertEqual(tampered.response_binding, original.response_binding)
                self.assertEqual(tampered.fingerprint, original.fingerprint)
                # Honest recomputation over the mutated content — exactly
                # what artifact serialization performs — diverges from the
                # carried digest, so the edit fails closed at persistence.
                recomputed = canonical_fingerprint(
                    {
                        "kind": "blind_judge_request",
                        "schema_version": SCHEMA_VERSION,
                        "prompt_version": PROMPT_VERSION,
                        "role": tampered.role,
                        "token": tampered.token,
                        "schema_name": SCHEMA_NAME,
                        "producer_fingerprint": tampered.producer_fingerprint,
                        "schema": dict(tampered.schema),
                        "prompt": tampered.prompt,
                    }
                )
                self.assertNotEqual(recomputed, tampered.fingerprint)

    def test_to_dict_exposes_identity_and_stays_independent(self):
        for request in build_requests():
            as_dict = request.to_dict()
            self.assertEqual(as_dict["producer_fingerprint"], PRODUCER_IDENTITY)
            self.assertEqual(as_dict["response_binding"], request.response_binding)
            as_dict["producer_fingerprint"] = OTHER_IDENTITY
            as_dict["response_binding"] = "0" * 64
            self.assertEqual(request.producer_fingerprint, PRODUCER_IDENTITY)
            self.assertEqual(request.response_binding, stated_response_binding(request))
            fresh = request.to_dict()
            self.assertEqual(fresh["producer_fingerprint"], PRODUCER_IDENTITY)
            self.assertEqual(fresh["response_binding"], request.response_binding)

    def test_severe_regression_fails_panel(self):
        flagged = []
        for request in self.requests:
            payload = valid_payload(request)
            if request.role == JUDGE_ROLES[0]:
                payload["severe_regression"] = True
                payload["severe_regression_reason"] = "contradicts filing"
            else:
                payload["concrete_defects"].append("minor wording drift")
            flagged.append(parse_judge_result(request, payload))
        report = aggregate_judge_panel(self.requests, flagged, passing_gate())
        self.assertFalse(report.passed)
        self.assertEqual(report.severe_regression_roles, (JUDGE_ROLES[0],))
        self.assertFalse(self._criterion(report, "no_severe_regression").passed)

    def test_one_abstaining_judge_fails_no_abstentions(self):
        results = [
            parse_judge_result(
                request,
                abstaining_payload(request)
                if request.role == JUDGE_ROLES[1]
                else valid_payload(request),
            )
            for request in self.requests
        ]
        aggregate_judge_panel(self.requests, results, passing_gate())

    def test_contradictory_gate_report_fails_closed(self):
        report = aggregate_judge_panel(
            self.requests, self.results, contradictory_gate()
        )
        self.assertFalse(report.passed)
        gates = self._criterion(report, "hard_gates_pass")
        self.assertFalse(gates.passed)
        self.assertEqual(len(report.gate_failures), 1)
        self.assertEqual(report.gate_failures[0].code, "metric_conflict")

    def test_malformed_gate_reports_fail_closed_without_raising(self):
        for variant in malformed_gate_variants():
            with self.subTest(variant=repr(variant)[:60]):
                report = aggregate_judge_panel(self.requests, self.results, variant)
                self.assertFalse(report.passed)
                self.assertFalse(self._criterion(report, "hard_gates_pass").passed)
                for criterion in report.criteria:
                    if criterion.criterion != "hard_gates_pass":
                        self.assertTrue(criterion.passed)

    def test_unidentifiable_gate_report_raises_as_cross_run_mixup(self):
        # Reports that cannot carry the run identity at all are not merely
        # failing gates: aggregation refuses them outright as cross-run
        # mixups instead of grading them as gate failures.
        for variant in (object(), None):
            with self.subTest(variant=repr(variant)[:60]):
                with self.assertRaises(ValueError):
                    aggregate_judge_panel(self.requests, self.results, variant)

    def test_gate_failure_summaries_bounded_and_structural(self):
        report = aggregate_judge_panel(
            self.requests,
            self.results,
            {
                "passed": False,
                "failures": [
                    {
                        "code": f"code_{index}",
                        "severity": "high",
                        "root_category": "arithmetic",
                        "path": f"facts.metrics.m{index}.value",
                        "evidence": f"evidence {index}",
                    }
                    for index in range(60)
                ],
                "producer_fingerprint": PRODUCER_IDENTITY,
            },
        )
        self.assertFalse(report.passed)
        self.assertEqual(len(report.gate_failures), 50)
        self.assertEqual(report.gate_failures[0].code, "code_0")
        self.assertEqual(report.gate_failures[49].code, "code_49")
        rendered = report.to_dict()
        self.assertEqual(len(rendered["gate_failures"]), 50)
        self.assertEqual(
            set(rendered["gate_failures"][0]),
            {"code", "severity", "root_category", "path", "evidence"},
        )

    def test_dataclass_like_gate_report_is_consumed_structurally(self):
        @dataclass(frozen=True)
        class GateFailureLike:
            code: str
            severity: str
            root_category: str
            path: str
            evidence: str

        @dataclass(frozen=True)
        class GateReportLike:
            passed: bool
            producer_fingerprint: str
            failures: tuple

        report_obj = GateReportLike(
            passed=False,
            producer_fingerprint=PRODUCER_IDENTITY,
            failures=(
                GateFailureLike(
                    code="required_evidence_absent",
                    severity="material",
                    root_category="filing_evidence",
                    path="facts.qualitative.guidance_up.evidence",
                    evidence="quoted sentence not present in excerpt",
                ),
            ),
        )
        report = aggregate_judge_panel(self.requests, self.results, report_obj)
        self.assertFalse(report.passed)
        self.assertFalse(self._criterion(report, "hard_gates_pass").passed)
        self.assertEqual(len(report.gate_failures), 1)
        failure = report.gate_failures[0]
        self.assertEqual(failure.code, "required_evidence_absent")
        self.assertEqual(failure.severity, "material")
        self.assertEqual(failure.root_category, "filing_evidence")
        self.assertEqual(failure.path, "facts.qualitative.guidance_up.evidence")
        self.assertIn("not present", failure.evidence)

    def test_dataclass_like_gate_report_with_contradictory_flag_fails(self):
        @dataclass(frozen=True)
        class GateReportLike:
            passed: bool
            producer_fingerprint: str
            failures: tuple

        contradiction = GateReportLike(
            passed=True,
            producer_fingerprint=PRODUCER_IDENTITY,
            failures=(object(),),
        )
        report = aggregate_judge_panel(self.requests, self.results, contradiction)
        self.assertFalse(report.passed)
        self.assertFalse(self._criterion(report, "hard_gates_pass").passed)
        self.assertEqual(len(report.gate_failures), 1)
        self.assertEqual(
            {
                key: getattr(report.gate_failures[0], key)
                for key in ("code", "severity", "root_category")
            },
            {"code": "unknown", "severity": "unknown", "root_category": "unknown"},
        )

    def test_panel_report_is_immutable_snapshot(self):
        report = aggregate_judge_panel(self.requests, self.results, passing_gate())
        self.assertIsInstance(report, JudgePanelReport)
        with self.assertRaises(FrozenInstanceError):
            report.overall_median = 5.0
        with self.assertRaises(TypeError):
            report.dimension_medians["hacked"] = 5.0
        with self.assertRaises(TypeError):
            report.new_field = True
        with self.assertRaises(FrozenInstanceError):
            report.producer_fingerprint = OTHER_IDENTITY

    def test_per_judge_floor_constant_and_criterion_shape(self):
        self.assertEqual(CORE_DIMENSION_JUDGE_FLOOR, 4.0)
        report = aggregate_judge_panel(self.requests, self.results, passing_gate())
        names = [criterion.criterion for criterion in report.criteria]
        self.assertIn("core_dimension_judge_floor", names)
        self.assertEqual(names[-1], "core_dimension_judge_floor")
        floor_criterion = self._criterion(report, "core_dimension_judge_floor")
        self.assertTrue(floor_criterion.passed)
        self.assertTrue(floor_criterion.detail)

    def test_unanimous_score_three_on_every_dimension_fails_per_judge_floor(self):
        # Redundant catch: a unanimous 3 breaks both the median criterion
        # (3.0 < 4.0) and the per-judge minimum (3 < 4).
        for dimension in JUDGE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                results = floored_results(self.requests, dimension)
                report = aggregate_judge_panel(self.requests, results, passing_gate())
                self.assertFalse(report.passed)
                self.assertEqual(report.dimension_medians[dimension], 3.0)
                self.assertEqual(report.dimension_minima[dimension], 3)
                self.assertFalse(
                    self._criterion(report, "all_dimension_medians").passed
                )
                floor_criterion = self._criterion(report, "core_dimension_judge_floor")
                self.assertFalse(floor_criterion.passed)
                self.assertIn(dimension, floor_criterion.detail)

    def test_single_low_judge_155_fails_floor_while_all_median_criteria_pass(self):
        # The non-compensable shape: scores (1, 5, 5) leave the median at
        # 5.0 — every median-based criterion clears — while one judge below
        # 4 fails the per-judge floor. Applied to all ten dimensions.
        named = {"factual_fidelity", "materiality", "counter_thesis_strength"}
        for dimension in JUDGE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                results = []
                for index, request in enumerate(self.requests):
                    payload = valid_payload(request)
                    if index == 0:
                        payload["dimension_scores"][dimension] = {
                            "score": 1,
                            "rationale": f"{dimension} contradicts the excerpt",
                        }
                        payload["concrete_defects"] = [
                            "summary states a margin the excerpt does not support"
                        ]
                    results.append(parse_judge_result(request, payload))
                report = aggregate_judge_panel(self.requests, results, passing_gate())
                self.assertFalse(report.passed)
                self.assertEqual(report.dimension_medians[dimension], 5.0)
                self.assertEqual(report.dimension_minima[dimension], 1)
                self.assertTrue(self._criterion(report, "all_dimension_medians").passed)
                if dimension in named:
                    self.assertTrue(
                        self._criterion(report, f"{dimension}_median").passed
                    )
                floor_criterion = self._criterion(report, "core_dimension_judge_floor")
                self.assertFalse(floor_criterion.passed)
                self.assertIn(dimension, floor_criterion.detail)

    def test_per_judge_floor_455_triple_may_pass_for_each_dimension(self):
        # Boundary: scores (4, 5, 5) put the minimum exactly on the floor;
        # the panel may pass.
        for dimension in JUDGE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                results = [
                    parse_judge_result(
                        request,
                        edge_floor_payload(request, dimension, 4 if index == 0 else 5),
                    )
                    for index, request in enumerate(self.requests)
                ]
                report = aggregate_judge_panel(self.requests, results, passing_gate())
                self.assertEqual(report.dimension_minima[dimension], 4)
                floor_criterion = self._criterion(report, "core_dimension_judge_floor")
                self.assertTrue(floor_criterion.passed)
                self.assertTrue(report.passed)

    def test_per_judge_floor_applies_across_all_ten_dimensions(self):
        # Each judge drops a DIFFERENT dimension to 3: every minimum lands
        # below the floor while every median stays 5.0 — only the per-judge
        # criterion can catch this shape.
        results = []
        for index, request in enumerate(self.requests):
            payload = valid_payload(request)
            dimension = JUDGE_DIMENSIONS[index % len(JUDGE_DIMENSIONS)]
            payload["dimension_scores"][dimension] = {
                "score": 3,
                "rationale": f"{dimension} graded 3 against the rubric anchors",
            }
            payload["concrete_defects"].append(
                "analysis asserts growth absent from the excerpt"
            )
            results.append(parse_judge_result(request, payload))
        report = aggregate_judge_panel(self.requests, results, passing_gate())
        floored_dimensions = {
            JUDGE_DIMENSIONS[index % len(JUDGE_DIMENSIONS)] for index in range(3)
        }
        for dimension in floored_dimensions:
            self.assertEqual(report.dimension_minima[dimension], 3)
        self.assertTrue(
            all(median == 5.0 for median in report.dimension_medians.values())
        )
        floor_criterion = self._criterion(report, "core_dimension_judge_floor")
        self.assertFalse(floor_criterion.passed)
        self.assertFalse(report.passed)

    def test_all_abstaining_judges_fail(self):
        results = [
            parse_judge_result(request, abstaining_payload(request))
            for request in self.requests
        ]
        report = aggregate_judge_panel(self.requests, results, passing_gate())
        self.assertEqual(set(report.abstained_roles), set(JUDGE_ROLES))
        self.assertFalse(self._criterion(report, "no_abstentions").passed)
        self.assertFalse(report.passed)

    def test_no_abstention_passes_when_other_thresholds_pass(self):
        report = aggregate_judge_panel(self.requests, self.results, passing_gate())
        abstention_criterion = self._criterion(report, "no_abstentions")
        self.assertTrue(abstention_criterion.passed)
        self.assertEqual(report.abstained_roles, ())
        self.assertTrue(report.passed)

    def test_cross_run_result_rejected(self):
        other_run = build_requests(SALT_B)
        target_role = self.requests[2].role
        # Pick the other run's request for exactly the displaced role so the
        # substituted result keeps a valid role while carrying that run's
        # token and response binding (guaranteed distinct under a fresh salt).
        foreign_request = next(
            request for request in other_run if request.role == target_role
        )
        foreign = parse_judge_result(foreign_request, valid_payload(foreign_request))
        mixed = [self.results[0], self.results[1], foreign]
        roles = [result.role for result in mixed]
        self.assertEqual(len(set(roles)), 3)
        self.assertNotEqual(foreign.token, self.requests[2].token)
        self.assertNotEqual(foreign.response_binding, self.requests[2].response_binding)
        with self.assertRaises(ValueError):
            aggregate_judge_panel(self.requests, mixed, passing_gate())

    def test_duplicate_result_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_judge_panel(
                self.requests,
                [*self.results, self.results[0]],
                passing_gate(),
            )

    def test_missing_result_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_judge_panel(self.requests, self.results[:2], passing_gate())

    def test_wrong_panel_shape_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_judge_panel(self.requests[:2], self.results[:2], passing_gate())


class FractionalScoreBoundaryTests(unittest.TestCase):
    """Tenth-step acceptance table plus overprecision/type/range/nonfinite
    rejection for the overall and every dimension.

    Under the retired integer schema every accepted decimal here raised
    ``ValueError`` ("must be an integer"), so this suite discriminates
    exactly the fractional contract; under it, no invalid shape may be
    silently rounded onto the grid.
    """

    def setUp(self):
        self.requests = build_requests()
        self.request = self.requests[0]

    def test_every_tenth_step_value_accepted_for_overall_and_dimensions(self):
        for value in tenth_step_values():
            with self.subTest(score=value):
                result = parse_judge_result(
                    self.request, scored_payload(self.request, value)
                )
                self.assertEqual(result.overall, value)
                self.assertTrue(all(score.score == value for score in result.scores))

    def test_extreme_grid_values_are_preserved_exactly(self):
        low = parse_judge_result(self.request, scored_payload(self.request, 1.0))
        high = parse_judge_result(self.request, scored_payload(self.request, 5.0))
        self.assertEqual(low.overall, 1.0)
        self.assertEqual(high.overall, 5.0)
        # Fractional grid points stay floats; they are never coerced onto
        # the integer lattice.
        self.assertEqual(type(high.overall), float)
        self.assertEqual(type(low.overall), float)
        mixed = parse_judge_result(
            self.request,
            scored_payload(self.request, 5, dimension_score=3.9),
        )
        self.assertEqual(mixed.overall, 5)
        self.assertEqual(mixed.score_for(JUDGE_DIMENSIONS[0]), 3.9)

    def _assert_score_rejected(self, payload_mutator):
        payload = valid_payload(self.request)
        payload_mutator(payload)
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, payload)

    def _set_overall(self, value):
        return lambda payload: payload.update({"overall": value})

    def _set_dimension(self, dimension, value):
        def mutate(payload):
            payload["dimension_scores"][dimension] = {
                "score": value,
                "rationale": f"{dimension} graded against the rubric anchors",
            }

        return mutate

    def test_invalid_values_rejected_for_overall(self):
        for value in invalid_score_values():
            with self.subTest(value=repr(value)):
                self._assert_score_rejected(self._set_overall(value))

    def test_invalid_values_rejected_for_every_dimension(self):
        for dimension in JUDGE_DIMENSIONS:
            for value in (4.15, "4.3", True, float("nan"), 5.05, -0.4):
                with self.subTest(dimension=dimension, value=repr(value)):
                    self._assert_score_rejected(self._set_dimension(dimension, value))

    def test_overprecise_literal_in_raw_json_is_not_rounded_onto_grid(self):
        # A raw JSON literal carrying more than one decimal digit must be
        # rejected at the literal level: rounding it to a grid point would
        # let judges claim scores they did not award.
        payload = valid_payload(self.request)
        payload["overall"] = None
        raw = json.dumps(payload).replace(
            '"overall": null', '"overall": 4.300000000000000001'
        )
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, raw)

    def test_float_alias_of_grid_point_is_accepted_without_coercion(self):
        payload = valid_payload(self.request)
        payload["overall"] = 4.6
        payload["dimension_scores"][JUDGE_DIMENSIONS[2]]["score"] = 4.6
        result = parse_judge_result(self.request, json.dumps(payload))
        # The stored value compares equal to the exact tenth and stays a
        # faithful float representation, not an integer substitute.
        self.assertEqual(result.overall, 4.6)
        self.assertEqual(type(result.overall), float)
        self.assertEqual(result.score_for(JUDGE_DIMENSIONS[2]), 4.6)
        self.assertNotIsInstance(result.overall, int)

    def test_integer_scores_remain_valid_exact_values(self):
        result = parse_judge_result(self.request, valid_payload(self.request))
        self.assertEqual(result.overall, 5)
        self.assertEqual(type(result.overall), int)
        self.assertTrue(all(type(s.score) is int for s in result.scores))

    def test_sub4_decimal_scores_require_rationale_and_defect(self):
        # Rationale is mandatory at any score; a located defect is required
        # exactly when the lowest awarded score lands below 4.0: 3.9 needs
        # one, 4.0 does not.
        for score, defects_required in ((3.9, True), (4.0, False), (3.0, True)):
            with self.subTest(score=score):
                result = parse_judge_result(
                    self.request,
                    scored_payload(
                        self.request,
                        score,
                        concrete_defects=(
                            ["summary asserts growth absent from the excerpt"]
                            if defects_required
                            else []
                        ),
                    ),
                )
                self.assertTrue(
                    all(s.rationale and s.rationale.strip() for s in result.scores)
                )
            if score < 4.0:
                with self.subTest(score=score, kind="missing-defect"):
                    with self.assertRaises(ValueError):
                        parse_judge_result(
                            self.request,
                            scored_payload(self.request, score, concrete_defects=[]),
                        )

    def test_blank_rationale_rejected_at_fractional_score(self):
        payload = scored_payload(self.request, 4.7)
        payload["dimension_scores"][JUDGE_DIMENSIONS[0]] = {
            "score": 4.7,
            "rationale": "   ",
        }
        with self.assertRaises(ValueError):
            parse_judge_result(self.request, payload)


class FractionalPanelThresholdTests(unittest.TestCase):
    """Exact-threshold reachability for 4.5/4.8/4.3 medians with 0.1-below
    failures, per-dimension criteria, decimal-safe comparisons, and
    sub-4.0 enforcement at panel level."""

    THRESHOLD_CASES = (
        ("factual_fidelity", FACTUAL_FIDELITY_MEDIAN_MINIMUM),
        ("materiality", MATERIALITY_MEDIAN_MINIMUM),
        ("counter_thesis_strength", COUNTER_THESIS_STRENGTH_MEDIAN_MINIMUM),
    )

    def setUp(self):
        self.requests = build_requests()

    def _criterion(self, report, name):
        return next(c for c in report.criteria if c.criterion == name)

    def test_threshold_constants_pin_fractional_policy(self):
        self.assertEqual(OVERALL_MEDIAN_MINIMUM, 4.5)
        self.assertEqual(DIMENSION_MEDIAN_MINIMUM, 4.0)
        for _, threshold in self.THRESHOLD_CASES:
            self.assertIsInstance(threshold, float)
            self.assertGreater(threshold, DIMENSION_MEDIAN_MINIMUM)
        self.assertEqual(COUNTER_THESIS_STRENGTH_MEDIAN_MINIMUM, 4.3)

    def test_named_medians_pass_exactly_at_threshold_and_fail_a_tenth_below(self):
        # Medians of three values are the middle value verbatim, so a
        # unanimous triple places the median exactly on each target.
        for name, threshold in self.THRESHOLD_CASES:
            on_grid = round(threshold, 1)
            below = round(threshold - 0.1, 1)
            with self.subTest(dimension=name, threshold=threshold):
                passing = panel_with_scores(self.requests, {name: (on_grid,) * 3})
                self.assertEqual(passing.dimension_medians[name], threshold)
                criterion = self._criterion(passing, f"{name}_median")
                self.assertTrue(criterion.passed)
                self.assertIn(str(threshold), criterion.detail)
                self.assertTrue(passing.passed)
                failing = panel_with_scores(self.requests, {name: (below,) * 3})
                self.assertEqual(failing.dimension_medians[name], below)
                self.assertLess(failing.dimension_medians[name], threshold)
                self.assertFalse(self._criterion(failing, f"{name}_median").passed)
                self.assertFalse(failing.passed)

    def test_split_panel_median_hits_each_threshold_exactly(self):
        # Non-unanimous shape: two judges award the exact threshold, the
        # third awards above it; the median still sits precisely on it.
        for name, threshold in self.THRESHOLD_CASES:
            on_grid = round(threshold, 1)
            with self.subTest(dimension=name):
                report = panel_with_scores(
                    self.requests, {name: (on_grid, on_grid, 5.0)}
                )
                self.assertEqual(report.dimension_medians[name], threshold)
                self.assertTrue(report.passed)

    def test_overall_median_boundary_is_decimal_safe(self):
        # Two judges award exactly 4.5; whether the panel passes depends on
        # the third judge alone: 4.6 lifts the median above the minimum,
        # 4.4 leaves it a tenth short. Equality meets the threshold.
        at = panel_with_overalls(self.requests, (4.5, 4.5, 4.5))
        self.assertEqual(at.overall_median, OVERALL_MEDIAN_MINIMUM)
        self.assertTrue(self._criterion(at, "panel_overall_median").passed)
        self.assertTrue(at.passed)
        above = panel_with_overalls(self.requests, (4.5, 4.5, 4.6))
        self.assertEqual(above.overall_median, 4.5)
        self.assertTrue(above.passed)
        below = panel_with_overalls(self.requests, (4.4, 4.4, 4.4))
        self.assertEqual(below.overall_median, 4.4)
        overall_criterion = self._criterion(below, "panel_overall_median")
        self.assertFalse(overall_criterion.passed)
        self.assertIn("4.4", overall_criterion.detail)
        self.assertFalse(below.passed)

    def test_generic_dimension_floor_with_decimals(self):
        name = JUDGE_DIMENSIONS[9]
        passing = panel_with_scores(self.requests, {name: (4.0, 4.1, 4.0)})
        self.assertEqual(passing.dimension_medians[name], 4.0)
        self.assertTrue(self._criterion(passing, "all_dimension_medians").passed)
        # A unanimous tenth-below triple puts the median itself below the
        # generic floor while every judge also breaches the per-judge floor.
        failing = panel_with_scores(self.requests, {name: (3.9, 3.9, 3.9)})
        self.assertEqual(failing.dimension_medians[name], 3.9)
        self.assertFalse(failing.passed)
        self.assertFalse(self._criterion(failing, "all_dimension_medians").passed)
        floor_criterion = self._criterion(failing, "core_dimension_judge_floor")
        self.assertFalse(floor_criterion.passed)

    def test_per_judge_floor_bounded_by_decimal_scores(self):
        # One judge at 3.9 fails the non-compensable floor even though the
        # median stays at 5.0; 4.0 exactly on the floor passes.
        for dimension in JUDGE_DIMENSIONS:
            with self.subTest(dimension=dimension):
                failing = panel_with_scores(self.requests, {dimension: (3.9, 5.0, 5.0)})
                self.assertFalse(
                    self._criterion(failing, "core_dimension_judge_floor").passed
                )
                self.assertFalse(failing.passed)
                passing = panel_with_scores(self.requests, {dimension: (4.0, 5.0, 5.0)})
                self.assertTrue(
                    self._criterion(passing, "core_dimension_judge_floor").passed
                )

    def test_all_ten_dimensions_simultaneously_on_their_thresholds(self):
        overrides = {
            name: (round(threshold, 1),) * 3 for name, threshold in self.THRESHOLD_CASES
        }
        report = panel_with_scores(self.requests, overrides)
        for name, threshold in self.THRESHOLD_CASES:
            self.assertEqual(report.dimension_medians[name], threshold)
            self.assertTrue(self._criterion(report, f"{name}_median").passed)
        self.assertTrue(report.passed)
        criteria = [c.criterion for c in report.criteria]
        self.assertEqual(len([n for n in criteria if n.endswith("_median")]), 4)

    def test_report_to_dict_preserves_decimal_scores_without_coercion(self):
        overrides = {
            name: (round(threshold, 1),) * 3 for name, threshold in self.THRESHOLD_CASES
        }
        report = panel_with_scores(self.requests, overrides)
        rendered = report.to_dict()
        self.assertEqual(rendered["overall_median"], 5.0)
        for name, threshold in self.THRESHOLD_CASES:
            self.assertEqual(rendered["dimension_medians"][name], threshold)
            stored = rendered["dimension_medians"][name]
            self.assertIsInstance(stored, float)
            self.assertNotIsInstance(stored, bool)
        self.assertEqual(
            json.loads(json.dumps(rendered))["dimension_medians"],
            dict(rendered["dimension_medians"]),
        )

    def test_decimal_minima_survive_aggregation(self):
        report = panel_with_scores(
            self.requests,
            {"evidence_selection": (3.9, 4.8, 5.0)},
        )
        self.assertEqual(report.dimension_minima["evidence_selection"], 3.9)
        self.assertIsInstance(report.dimension_minima["evidence_selection"], float)


if __name__ == "__main__":
    unittest.main()
