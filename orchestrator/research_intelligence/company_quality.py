"""Deterministic, non-compensable hard gates for company-benchmark runs.

Every gate inspects only the finalized producer replay and the evaluator
expectations.  Failures are structured, evidence-backed, and never offset by
any other dimension: a report passes if and only if it carries zero failures.
No subjective weighted score exists here by design — subjective judgement
belongs to the blind judge panel, which must never see gate results inside its
request packets.

``root_category`` values are drawn from existing repository taxonomies only:
``investment_service.VALIDATION_JSON_SCHEMA`` / ``VALIDATION_FILING_EVIDENCE``
for contract/schema and filing-grounding failures, ``errors``-style
``invalid_source_data`` for point-in-time and fingerprint contract breaches,
and the ``processors._validators.PROHIBITED_PATTERNS`` categories for
prohibited-language findings.

Severity policy: record-integrity breaches (fabrication-class evidence
violations, hindsight leakage, prohibited instructions, fingerprint mismatch,
malformed evaluator ledgers, arithmetic mistakes) are ``critical``; missing or
unsupported material content is ``material``.  Severity ranks the rendered
report only — every failure is equally non-compensable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from investment_service import (
    QUALITATIVE_NAMES,
    VALIDATION_FILING_EVIDENCE,
    VALIDATION_JSON_SCHEMA,
    filing_content_spans,
    investment_evidence_violations,
)

import math
from collections.abc import Mapping

from processors._validators import scan_prohibited_language
from research_intelligence.company_benchmarks import EvaluatorCase, ProducerCase
import investment_service
from investment_engine import build_material_relationship_contract


if TYPE_CHECKING:
    from investment_service import InvestmentFinalizedAnalysis

SEVERITY_CRITICAL = "critical"
SEVERITY_MATERIAL = "material"
SEVERITIES = (SEVERITY_CRITICAL, SEVERITY_MATERIAL)

ROOT_CONTRACT = VALIDATION_JSON_SCHEMA
ROOT_EVIDENCE = VALIDATION_FILING_EVIDENCE
ROOT_SOURCE_DATA = "invalid_source_data"

_MAX_PATH_CHARS = 300
_MAX_TEXT_CHARS = 400
_MAX_NEWS_SCAN_CHARS = 2_000_000
_MAX_ALIAS_SCAN_CHARS = 200

_LEDGER_COMMON_KEYS = frozenset({"check_id", "kind", "path", "severity", "rationale"})
_LEDGER_EXPECTED_KEYS = frozenset({"expected"})
_LEDGER_TOLERANCE_KEYS = frozenset({"tolerance"})
_LEDGER_ARITHMETIC_KEYS = frozenset(
    {"numerator_path", "denominator_path", "scale", "expected_path", "tolerance"}
)
_LEDGER_KINDS = frozenset(
    {"equals", "contains", "not_contains", "nonblank", "number_close", "arithmetic_close"}
)
_FISCAL_QUARTER_PERIOD_RES = (
    re.compile(
        r"\bfy\s*(?P<year>\d{4}|\d{2})(?:\s*-\s*|\s+)"
        r"q(?P<quarter>[1-4])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bq(?P<quarter>[1-4])(?:\s*-\s*|\s+)"
        r"fy\s*(?P<year>\d{4}|\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfy\s*(?P<year>\d{4}|\d{2})(?:\s*-\s*|\s+)"
        r"(?P<quarter>first|second|third|fourth)\s+quarter\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfiscal(?:\s+year)?\s+(?P<year>\d{4})(?:\s*-\s*|\s+)"
        r"(?P<quarter>q[1-4]|first|second|third|fourth)"
        r"(?:\s+quarter)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<quarter>first|second|third|fourth)\s+quarter"
        r"(?:\s+of)?\s+fiscal(?:\s+year)?\s+(?P<year>\d{4})\b",
        re.IGNORECASE,
    ),
)
_FISCAL_YEAR_PERIOD_RE = re.compile(
    r"\b(?:fy\s*(?P<short>\d{4}|\d{2})|"
    r"fiscal(?:\s+year)?\s+(?P<long>\d{4}))\b",
    re.IGNORECASE,
)
_CALENDAR_QUARTER_PERIOD_RES = (
    re.compile(
        r"\bq(?P<quarter>[1-4])\s+(?P<year>(?:19|20)\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<year>(?:19|20)\d{2})\s+q(?P<quarter>[1-4])\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<quarter>first|second|third|fourth)\s+quarter"
        r"(?:\s+of)?\s+(?P<year>(?:19|20)\d{2})\b",
        re.IGNORECASE,
    ),
)
_RELATIVE_PERIOD_RE = re.compile(
    r"\b(?P<direction>next|following|forward|current|this|prior|previous|last)"
    r"\s+(?:(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve)\s+)?"
    r"(?P<unit>days?|weeks?|months?|quarters?|years?)\b",
    re.IGNORECASE,
)
_COMPARATIVE_PERIOD_RE = re.compile(
    r"\b(?P<kind>year[\s-]+over[\s-]+year|quarter[\s-]+over[\s-]+quarter|"
    r"month[\s-]+over[\s-]+month|yoy|qoq|mom|sequential(?:ly)?)\b",
    re.IGNORECASE,
)
_COMPARISON_BASIS_PERIOD_LABELS = frozenset(
    {
        "relative:year-over-year",
        "relative:quarter-over-quarter",
        "relative:month-over-month",
    }
)
_QUARTER_NUMBER = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
_NUMBER_WORD = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}
_PATH_LABEL_RE = re.compile(r"^[A-Za-z0-9_$.\[\]-]+$")


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One non-compensable hard-gate violation."""

    code: str
    severity: str
    root_category: str
    path: str
    observed: Any
    expected: Any
    evidence: str



@dataclass(frozen=True, slots=True)
class HardGateReport:
    """Pass/fail outcome over an ordered tuple of gate failures.

    ``producer_fingerprint`` is the required SHA-256 identity of the producer
    case this report was computed against; reports from different runs are
    never interchangeable.
    """

    passed: bool
    producer_fingerprint: str
    failures: tuple[GateFailure, ...]

    def __post_init__(self) -> None:
        _required_producer_fingerprint(self.producer_fingerprint)


_PRODUCER_FINGERPRINT_RE = re.compile(r"[a-f0-9]{64}")


def _required_producer_fingerprint(value: object) -> str:
    """Validate one producer fingerprint; malformed identity fails closed."""
    if not isinstance(value, str) or not _PRODUCER_FINGERPRINT_RE.fullmatch(value):
        raise ValueError("producer fingerprint must be nonblank SHA-256 hex")
    return value


def _fail(
    *,
    code: str,
    severity: str,
    root_category: str,
    path: str,
    observed: Any = None,
    expected: Any = None,
    evidence: str,
) -> GateFailure:
    return GateFailure(
        code=code,
        severity=severity,
        root_category=root_category,
        path=path[:_MAX_PATH_CHARS],
        observed=_bound(observed),
        expected=_bound(expected),
        evidence=evidence[:_MAX_TEXT_CHARS],
    )


def _bound(value: Any) -> Any:
    """Bounded, JSON-native copy of one failure payload field.

    Frozen packet containers (``MappingProxyType``/tuples) are materialized
    into plain dicts/lists so every rendered report serializes directly.
    """
    if isinstance(value, str):
        return value[:_MAX_TEXT_CHARS]
    if isinstance(value, Mapping):
        return {str(key): _bound(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bound(item) for item in value]
    return value


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_number(raw: str) -> str | None:
    """Canonical coefficient of one displayed numeric surface."""
    cleaned = raw.strip()
    for suffix in (
        "basis points",
        "percentage points",
        "trillions",
        "trillion",
        "billions",
        "billion",
        "millions",
        "million",
        "thousands",
        "thousand",
        "bps",
        "bns",
        "bn",
        "mns",
        "mn",
        "%",
        "bp",
        "t",
        "b",
        "m",
        "k",
        "x",
    ):
        if cleaned.casefold().endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    cleaned = cleaned.replace(",", "")
    cleaned = re.sub(r"[$€£¥\s]", "", cleaned)
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _claim_coefficient_key(value: object, unit: str) -> str | None:
    return investment_service._numeric_claim_coefficient_key(value, unit)




def _to_decimal(value: Any) -> Decimal | None:
    """Strict numeric coercion for ledger comparisons; ``None`` when not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        canonical = _normalize_number(value)
        if canonical is None:
            return None
        try:
            return Decimal(canonical)
        except InvalidOperation:
            return None
    return None


def _numeric_tokens(text: str) -> set[str]:
    """Normalized displayed coefficients, including compact scale suffixes."""
    return {
        token
        for match in investment_service._MATERIAL_DISPLAYED_NUMBER_RE.finditer(
            text
        )
        if (token := _normalize_number(match.group(0))) is not None
    }


def _iter_strings(node: Any, path: str = "$"):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, child in node.items():
            yield from _iter_strings(child, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, child in enumerate(node):
            yield from _iter_strings(child, f"{path}[{index}]")


def _collect_numeric_facts(node: Any, out: set[str]) -> None:
    """Admit every numeric token in source facts into the grounded set.

    A signed fact also admits its unsigned magnitude: a deterministic value of
    ``-0.06`` legitimately appears in narrative as "negative $0.06", where the
    word "negative" carries the sign and the bare magnitude is the quantity.
    Sign is never dropped anywhere else, so "-0.06" in output still requires
    the signed fact, and a different magnitude ("-0.07") stays ungrounded.
    """
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, (int, float, Decimal)):
        token = _normalize_number(str(node))
        if token is not None:
            out.add(token)
            if token.startswith("-"):
                out.add(token[1:])
        return
    if isinstance(node, str):
        # One explicit pass: every numeric token is admitted, and each signed
        # token also admits its unsigned magnitude ("negative $0.06" narrative
        # grounds against a "-0.06" string fact).
        for match in investment_service._MATERIAL_DISPLAYED_NUMBER_RE.finditer(node):
            token = _normalize_number(match.group(0))
            if token is None:
                continue
            out.add(token)
            if token.startswith("-"):
                out.add(token[1:])
        return
    if isinstance(node, Mapping):
        for child in node.values():
            _collect_numeric_facts(child, out)
        return
    if isinstance(node, (list, tuple)):
        for child in node:
            _collect_numeric_facts(child, out)


def _narrative_root_keys() -> frozenset[str]:
    """Top-level model-authored fields, taken from the production schema."""
    from investment_service import _response_schema

    properties = _response_schema()["schema"]["properties"]
    return frozenset(properties)


def _authored_projection(facts: object) -> dict:
    if not isinstance(facts, dict):
        return {}
    keys = _narrative_root_keys()
    return {key: value for key, value in facts.items() if key in keys}




def _fingerprint_failure(producer: ProducerCase, evaluator: EvaluatorCase) -> list[GateFailure]:
    if evaluator.producer_fingerprint == producer.fingerprint:
        return []
    return [
        _fail(
            code="producer_evaluator_fingerprint_mismatch",
            severity=SEVERITY_CRITICAL,
            root_category=ROOT_SOURCE_DATA,
            path="$.producer_fingerprint",
            observed=evaluator.producer_fingerprint,
            expected=producer.fingerprint,
            evidence="evaluator producer_fingerprint does not equal the producer case fingerprint",
        )
    ]


def _production_evidence_failures(
    producer: ProducerCase, finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    violations = investment_evidence_violations(
        finalized.facts,
        excerpt=producer.excerpt,
        news_items=[dict(item) for item in producer.news_items],
    )
    failures: list[GateFailure] = []
    for violation in violations:
        label, _, remainder = violation.partition(": ")
        path = label if _PATH_LABEL_RE.fullmatch(label) else "$"
        failures.append(
            _fail(
                code="investment_evidence_violation",
                severity=SEVERITY_CRITICAL,
                root_category=ROOT_EVIDENCE,
                path=f"$.{path}" if not path.startswith("$") else path,
                observed=violation,
                expected="every presented evidence quote grounded in one filing content span",
                evidence=violation,
            )
        )
        del remainder
    return failures


def _production_contract_failures(
    producer: ProducerCase, finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    """Replay the exact live Narrative v7 schema and relationship contract."""
    relationship_contract = build_material_relationship_contract(
        producer.deterministic_current,
        producer.deterministic_prior,
    ).to_payload()
    authored = _authored_projection(finalized.facts)
    problems = investment_service.validate_investment_report_payload(authored)
    problems.extend(
        investment_service.relationship_reconciliation_problems(
            authored,
            material_relationships=relationship_contract["material_relationships"],
        )
    )
    failures: list[GateFailure] = []
    for problem in problems:
        label, separator, _ = problem.partition(": ")
        path = label if separator and _PATH_LABEL_RE.fullmatch(label) else "$"
        failures.append(
            _fail(
                code="investment_narrative_contract_violation",
                severity=SEVERITY_CRITICAL,
                root_category=ROOT_CONTRACT,
                path=path if path.startswith("$") else f"$.{path}",
                observed=problem,
                expected="finalized output matching the Narrative v7 live contract",
                evidence=problem,
            )
        )
    return failures


def _required_evidence_failures(
    producer: ProducerCase, evaluator: EvaluatorCase
) -> list[GateFailure]:
    """Evaluator-fixture/retrieval integrity only: every required quote must
    exist inside one producer filing span. Output omission or paraphrase of a
    required quote is judged by expected_material_observations and the blind
    materiality judges, never by this hard pipeline gate.
    """
    from investment_service import _normalize_grounding_text

    failures: list[GateFailure] = []
    spans = [_normalize_grounding_text(span) for span in filing_content_spans(producer.excerpt)]
    for quote in evaluator.required_material_evidence:
        normalized = _normalize_grounding_text(quote)
        if not normalized:
            failures.append(
                _fail(
                    code="required_material_evidence_invalid",
                    severity=SEVERITY_CRITICAL,
                    root_category=ROOT_CONTRACT,
                    path="$.required_material_evidence",
                    observed=quote,
                    expected="nonblank required material evidence quote",
                    evidence="required_material_evidence row is blank after normalization",
                )
            )
            continue
        if not any(normalized in span for span in spans):
            failures.append(
                _fail(
                    code="required_evidence_absent_from_filing_span",
                    severity=SEVERITY_MATERIAL,
                    root_category=ROOT_EVIDENCE,
                    path="$.excerpt",
                    observed=normalized,
                    expected="normalized quote contained within one filing content span",
                    evidence=f"required quote not found in any single filing span: {normalized}",
                )
            )
    return failures


def _numeric_claim_ledger(
    finalized: "InvestmentFinalizedAnalysis",
) -> list[dict]:
    """Model-authored ledger rows from producer facts, never merged analysis."""
    rows = finalized.facts.get("numeric_claims")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]



def _numeric_grounding_failures(
    producer: ProducerCase, finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    """Per-claim grounding: every authored material number needs exactly one
    valid binding row whose target AND source pointers resolve against this
    frozen case with a compatible claim tuple (value/metric/period/unit/
    currency/scale, plus verified operation identity where applicable).
    """
    failures: list[GateFailure] = []
    failures.extend(_structural_claim_failures(finalized))
    failures.extend(_claim_coverage_failures(producer, finalized))
    return failures


def _structural_claim_failures(
    finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    """Shared structural row validation at the hard-gate boundary plus
    forged, stale, or ineligible target pointers.

    ``validate_numeric_claim_rows`` is the exact structural seam that
    response validation runs; re-running it here means malformed rows
    (including duplicate ``claim_id`` values across different paths) fail
    the gate even when a payload never crossed that seam. Independently, a
    row whose target is not an eligible narrative text leaf inside this
    finalized payload fails closed even when the narrative would pass.
    """
    failures: list[GateFailure] = []
    rows = finalized.facts.get("numeric_claims")
    for problem in investment_service.validate_numeric_claim_rows(rows):
        failures.append(
            _fail(
                code="numeric_claim_invalid_row",
                severity=SEVERITY_CRITICAL,
                root_category=ROOT_CONTRACT,
                path="numeric_claims",
                observed=problem,
                expected=(
                    "every numeric_claims row satisfies the structural "
                    "ledger contract"
                ),
                evidence=problem,
            )
        )
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        where = f"numeric_claims[{index}]"
        claim_id = row.get("claim_id")
        label = claim_id if isinstance(claim_id, str) and claim_id.strip() else where
        target_path = row.get("path")
        if isinstance(target_path, str) and target_path.strip():
            _, eligible = investment_service._resolve_numeric_claim_target(
                finalized.facts,
                target_path,
            )
            if not eligible:
                failures.append(
                    _fail(
                        code="numeric_claim_target_missing",
                        severity=SEVERITY_CRITICAL,
                        root_category=ROOT_CONTRACT,
                        path=where,
                        observed=target_path,
                        expected=(
                            "target path resolves to an eligible narrative "
                            "text leaf in the authored output"
                        ),
                        evidence=(
                            f"{where} ({label}): target path {target_path!r} "
                            "is not an eligible narrative text leaf in the "
                            "finalized payload"
                        ),
                    )
                )
    return failures


def _claim_source_failure(
    where: str,
    label: str,
    code: str,
    detail: str,
) -> GateFailure:
    return _fail(
        code=code,
        severity=SEVERITY_CRITICAL,
        root_category=ROOT_EVIDENCE,
        path=where,
        observed={"claim_id": label},
        expected="row resolves against this frozen producer case",
        evidence=f"{where} ({label}): {detail}",
    )

def _normalized_metric_name(value: object) -> str:
    """Stable identity used only to distinguish same-number target bindings."""
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

def _normalized_unit_name(value: object) -> str:
    """Canonical ledger-unit identity for duplicate detection."""
    return str(value or "").strip().casefold()





def _target_occurrence_compatible(
    row: Mapping[str, Any],
    text: str,
    start: int,
    end: int,
) -> bool:
    """Delegate occurrence-local target tuple semantics to production."""
    return investment_service._numeric_claim_target_occurrence_compatible(
        row,
        text,
        start,
        end,
    )



def _verify_claim_row(
    producer: ProducerCase,
    index: int,
    row: dict,
    fact_roots: Mapping[str, Any],
    deterministic_current: object,
    deterministic_prior: object,
) -> list[GateFailure]:
    """Resolve one row's source pointer and check its claim tuple.

    text rows: the quote must be verbatim inside one producer-visible
    surface (excerpt or recorded news); unit rendering, metric alias, and
    the same displayed coefficient must co-occur in the bound source span,
    and the period must match that source span. fact rows: the pointer must
    resolve into the deterministic current/prior metrics and the row tuple
    must match that fact leaf's value/unit/currency/period. arithmetic rows:
    a producer-derived output fact must explicitly declare the operation and
    operands, whose dimensions and recomputed output must match the row.

    Range endpoints are independent displayed coefficients, so each endpoint
    can be bound and verified by its own row.
    """
    where = f"numeric_claims[{index}]"
    claim_id = row.get("claim_id")
    label = claim_id if isinstance(claim_id, str) and claim_id.strip() else where
    claimed_value = investment_service._canonical_claim_number(row.get("value"))
    if claimed_value is None:
        return [
            _claim_source_failure(
                where,
                label,
                "numeric_claim_source_unresolved",
                "value does not normalize to a finite quantity",
            )
        ]

    def mismatch(detail: str) -> list[GateFailure]:
        return [_claim_source_failure(
            where, label, "numeric_claim_tuple_mismatch", detail
        )]

    def unresolved(detail: str) -> list[GateFailure]:
        return [_claim_source_failure(
            where, label, "numeric_claim_source_unresolved", detail
        )]

    source_kind = row.get("source_kind")
    if source_kind == "fact":
        tuple_problem = investment_service._numeric_fact_claim_tuple_problem(
            row,
            fact_roots,
            deterministic_current,
            deterministic_prior,
        )
        if tuple_problem is None:
            return []
        if tuple_problem.kind == "unresolved":
            return unresolved(tuple_problem.detail)
        return mismatch(tuple_problem.detail)

    if source_kind == "text":
        tuple_problem = investment_service._numeric_text_claim_tuple_problem(
            row,
            fact_roots,
            excerpt=producer.excerpt,
            news_items=producer.news_items,
            document_metadata=producer.document,
        )
        if tuple_problem is None:
            return []
        if tuple_problem.kind == "unresolved":
            return unresolved(tuple_problem.detail)
        return mismatch(tuple_problem.detail)


    if source_kind == "arithmetic":
        tuple_problem = investment_service._numeric_arithmetic_claim_tuple_problem(
            row,
            fact_roots,
            deterministic_current,
            deterministic_prior,
        )
        if tuple_problem is None:
            return []
        if tuple_problem.kind == "unresolved":
            return unresolved(tuple_problem.detail)
        if tuple_problem.kind == "operation_unverified":
            return [
                _claim_source_failure(
                    where,
                    label,
                    "numeric_claim_operation_unverified",
                    tuple_problem.detail,
                )
            ]
        return mismatch(tuple_problem.detail)

    return unresolved(f"unknown source_kind {source_kind!r}")

def _relationship_numeric_coverage_failures(
    finalized: "InvestmentFinalizedAnalysis",
    relationship_contract: Mapping[str, Any],
    valid_bindings: Mapping[str, list[dict]],
) -> list[GateFailure]:
    """Require every compatible normalized numeric fact in its observation."""
    facts = relationship_contract.get("relationship_facts")
    relationships = relationship_contract.get("material_relationships")
    reconciliations = finalized.facts.get("relationship_reconciliations")
    if not isinstance(facts, Mapping) or not isinstance(relationships, (list, tuple)):
        return []
    authored = (
        {
            row.get("relationship_id"): row
            for row in reconciliations
            if isinstance(row, Mapping)
            and isinstance(row.get("relationship_id"), str)
        }
        if isinstance(reconciliations, list)
        else {}
    )
    failures: list[GateFailure] = []
    for index, relationship in enumerate(relationships):
        if (
            not isinstance(relationship, Mapping)
            or relationship.get("compatibility") != "compatible"
        ):
            continue
        relationship_id = relationship.get("relationship_id")
        row = authored.get(relationship_id)
        observation = (
            row.get("observation")
            if isinstance(row, Mapping)
            and isinstance(row.get("observation"), str)
            else ""
        )
        path = f"$.relationship_reconciliations[{index}].observation"
        target = investment_service._normalize_claim_path(path)
        bindings = valid_bindings.get(target, [])
        for ref in relationship.get("required_facts", ()):
            if not isinstance(ref, Mapping):
                continue
            fact_path = ref.get("fact_path")
            if not isinstance(fact_path, str):
                continue
            fact = facts.get(fact_path) or facts.get(fact_path.rsplit(".", 1)[-1])
            if not isinstance(fact, Mapping):
                continue
            unit = str(fact.get("unit") or "")
            fact_key = _claim_coefficient_key(fact.get("value"), unit)
            occurrences = (
                [
                    (start, end)
                    for start, end, token in investment_service.material_numeric_tokens(
                        observation
                    )
                    if token == fact_key
                ]
                if fact_key is not None
                else []
            )
            covered = (
                sum(
                    binding.get("source_kind") == "fact"
                    and binding.get("fact_path") == fact_path
                    and _claim_coefficient_key(
                        binding.get("value"), str(binding.get("unit") or "")
                    )
                    == fact_key
                    and any(
                        _target_occurrence_compatible(
                            binding, observation, start, end
                        )
                        for start, end in occurrences
                    )
                    for binding in bindings
                )
                == 1
            )
            if covered:
                continue
            failures.append(
                _fail(
                    code="numeric_claim_unbound",
                    severity=SEVERITY_MATERIAL,
                    root_category=ROOT_EVIDENCE,
                    path=path,
                    observed={
                        "fact_path": fact_path,
                        "value": _bound(fact.get("value")),
                    },
                    expected=(
                        "one valid numeric_claims fact row binding this "
                        "required normalized relationship fact in observation"
                    ),
                    evidence=(
                        f"compatible relationship {relationship_id!r} has no "
                        f"exact observation binding for {fact_path!r}"
                    ),
                )
            )
    return failures




def _claim_coverage_failures(
    producer: ProducerCase, finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    """Per-claim resolution plus coverage.

    Every ledger row must resolve against THIS frozen case with a compatible
    claim tuple; every authored material number outside evidence quotes must
    be covered by a row targeting its own path with the same canonical
    coefficient. A token whose row exists but fails verification reports only
    that row's failure — never an extra ``numeric_claim_unbound``.
    """
    failures: list[GateFailure] = []
    rows = _numeric_claim_ledger(finalized)
    relationship_contract = build_material_relationship_contract(
        producer.deterministic_current,
        producer.deterministic_prior,
    ).to_payload()
    deterministic_current, deterministic_prior = (
        investment_service._numeric_claim_fact_roots(
            producer.deterministic_current,
            producer.deterministic_prior,
            relationship_contract["relationship_facts"],
        )
    )
    _, relationship_missing_bindings = (
        investment_service._relationship_numeric_claim_findings(
            finalized.facts,
            rows,
            relationship_contract["relationship_facts"],
            relationship_contract["material_relationships"],
            deterministic_current,
            deterministic_prior,
        )
    )
    summary_binding_problems, summary_missing_keys = (
        investment_service._relationship_summary_numeric_claim_findings(
            finalized.facts,
            rows,
            relationship_contract["relationship_facts"],
            relationship_contract["material_relationships"],
            deterministic_current,
            deterministic_prior,
        )
    )
    for problem in summary_binding_problems:
        failures.append(
            _fail(
                code="numeric_claim_unbound",
                severity=SEVERITY_MATERIAL,
                root_category=ROOT_CONTRACT,
                path="$.summary",
                observed=problem,
                expected=(
                    "exactly one deduplicated valid summary fact row for each "
                    "unique selected relationship fact"
                ),
                evidence=problem,
            )
        )
    # Source verification remains replay-specific rendering; the shared pure
    # helper owns occurrence-sensitive authored coverage for both paths.
    structural_seen_ids: set[str] = set()
    structurally_valid_indexes = {
        index
        for index, row in enumerate(rows)
        if not investment_service._numeric_claim_row_problems(
            row, index, structural_seen_ids
        )
    }
    valid_row_indexes: set[int] = set()
    seen_bindings: dict[tuple[object, ...], str] = {}
    valid_bindings: dict[str, list[dict]] = {}
    for index, row in enumerate(rows):
        target = investment_service._normalize_claim_path(row.get("path") or "")
        unit = str(row.get("unit") or "")
        claimed_key = _claim_coefficient_key(row.get("value"), unit)
        semantic_key = investment_service._numeric_claim_semantic_binding_key(row)
        prior = (
            seen_bindings.get(semantic_key)
            if semantic_key is not None
            else None
        )
        if prior is not None:
            failures.append(_fail(
                code="numeric_claim_duplicate",
                severity=SEVERITY_CRITICAL,
                root_category=ROOT_CONTRACT,
                path=f"numeric_claims[{index}]",
                observed={"claim_id": row.get("claim_id")},
                expected="one ledger row per unique target/source semantic binding",
                evidence=(
                    f"numeric_claims[{index}] duplicates the semantic binding "
                    f"already carried by {prior}"
                ),
            ))
        elif semantic_key is not None:
            seen_bindings[semantic_key] = f"numeric_claims[{index}]"
        if not target or claimed_key is None or not _target_resolves(finalized, row):
            continue
        row_failures = _verify_claim_row(
            producer,
            index,
            row,
            finalized.facts,
            deterministic_current,
            deterministic_prior,
        )
        if row_failures:
            failures.extend(row_failures)
            continue
        if index in structurally_valid_indexes:
            valid_row_indexes.add(index)
            valid_bindings.setdefault(target, []).append(row)
    for finding in investment_service.numeric_claim_coverage_findings(
        finalized.facts,
        rows,
        valid_row_indexes=valid_row_indexes,
        invalid_row_indexes=set(range(len(rows))) - valid_row_indexes,
        specific_finding_keys=(
            investment_service._relationship_numeric_target_keys(
                relationship_contract["relationship_facts"],
                relationship_contract["material_relationships"],
                relationship_missing_bindings,
            )
            | summary_missing_keys
        ),
    ):
        failures.append(
            _fail(
                code="numeric_claim_unbound",
                severity=SEVERITY_MATERIAL,
                root_category=ROOT_CONTRACT,
                path=finding.path,
                observed=finding.coefficient,
                expected=(
                    "one valid numeric_claims row binding this number to "
                    "its exact producer source (text quote, deterministic "
                    "fact, or verified operation)"
                ),
                evidence=(
                    f"material number {finding.coefficient!r} at "
                    f"{finding.path} has no covering ledger row: "
                    f"{finding.snippet}"
                ),
            )
        )
    failures.extend(
        _relationship_numeric_coverage_failures(
            finalized,
            relationship_contract,
            valid_bindings,
        )
    )
    return failures


def _target_resolves(
    finalized: "InvestmentFinalizedAnalysis", row: dict
) -> bool:
    """Does the row target an eligible narrative text leaf?"""
    _, eligible = investment_service._resolve_numeric_claim_target(
        finalized.facts,
        str(row.get("path") or ""),
    )
    return eligible


def _normalized_forbidden_value(value: int | float | str) -> str | None:
    """Canonical numeric form of one forbidden claim value."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _normalize_number(repr(value) if isinstance(value, float) else str(value))
    return _normalize_number(str(value))


def _hindsight_failures(
    evaluator: EvaluatorCase, finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    """Fail only when ONE authored string carries metric + value + period.

    Per-string matching catches paraphrases of a specific post-``as_of``
    claim while never tripping on case-wide value collisions (the same
    number appearing elsewhere in unrelated narrative).
    """
    failures: list[GateFailure] = []
    for claim in evaluator.forbidden_hindsight:
        value = _normalized_forbidden_value(claim.value)
        metrics = {
            normalized
            for alias in claim.metric_aliases
            if len(alias) <= _MAX_ALIAS_SCAN_CHARS
            and (normalized := _collapse(alias).casefold())
        }
        periods = {
            normalized
            for alias in claim.period_aliases
            if len(alias) <= _MAX_ALIAS_SCAN_CHARS
            and (normalized := _collapse(alias).casefold())
        }
        for path, text in _iter_strings(_authored_projection(finalized.facts)):
            folded = text.casefold()
            has_metric = any(alias in folded for alias in metrics)
            if not has_metric or value is None:
                continue
            has_value = value in _numeric_tokens(folded)
            if not has_value:
                continue
            has_period = any(alias in folded for alias in periods)
            if not has_period:
                continue
            failures.append(
                _fail(
                    code="forbidden_company_claim_present",
                    severity=SEVERITY_CRITICAL,
                    root_category=ROOT_SOURCE_DATA,
                    path=path,
                    observed={
                        "claim_id": claim.claim_id,
                        "metric_alias": next(
                            alias for alias in metrics if alias in folded
                        ),
                        "period_alias": next(
                            alias for alias in periods if alias in folded
                        ),
                    },
                    expected=(
                        f"forbidden company claim '{claim.claim_id}' "
                        f"(available_after {claim.available_after.isoformat()}) "
                        "must not appear in output narrative"
                    ),
                    evidence=(
                        f"authored string at {path} combines a metric alias, the "
                        f"forbidden value {value}, and a period alias from claim "
                        f"'{claim.claim_id}'"
                    ),
                )
            )
    return failures


def _prohibited_language_failures(
    finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    failures: list[GateFailure] = []
    for finding in scan_prohibited_language(_authored_projection(finalized.facts)):
        path_part, _, remainder = finding.partition(": ")
        category = "prohibited_language"
        match = re.search(r"prohibited (\S+) language", remainder)
        if match:
            category = match.group(1)
        failures.append(
            _fail(
                code="prohibited_language_present",
                severity=SEVERITY_CRITICAL,
                root_category=category,
                path=path_part if path_part else "$",
                observed=finding,
                expected="no prohibited advisory/instructional language in output",
                evidence=finding,
            )
        )
    return failures


def _resolve_path(root: dict, dotted: str) -> tuple[Any, bool]:
    """Resolve a dotted path through dicts/lists only; never attributes.

    Accepts ``a.b``, ``a.0.b`` (list index as a segment) and ``items[0].name``
    (bracket index). Any other traversal — attribute access, function calls,
    string slicing — is rejected: the value simply does not resolve.
    """
    node: Any = root
    trimmed = dotted[2:] if dotted.startswith("$.") else dotted.lstrip("$")
    if trimmed.startswith("["):
        raise ValueError("path must start with a key name")
    for raw_part in trimmed.split("."):
        match = re.fullmatch(r"([^\[\]]+)\[(\d+)\]", raw_part)
        if match:
            name, index_text = match.group(1), match.group(2)
            segments: tuple[str, ...] = (name, index_text)
        else:
            if "[" in raw_part or "]" in raw_part:
                raise ValueError(f"path segment {raw_part!r} is not a supported selector")
            segments = (raw_part,)
        for part in segments:
            if isinstance(node, dict):
                if part in node:
                    node = node[part]
                    continue
                return None, False
            if isinstance(node, list):
                if re.fullmatch(r"\d+", part):
                    index = int(part)
                    if index < len(node):
                        node = node[index]
                        continue
                return None, False
            return None, False
    return node, True


def _ledger_contract_failure(index: int, detail: str, observed: Any) -> GateFailure:
    return _fail(
        code="deterministic_checks_contract_violation",
        severity=SEVERITY_CRITICAL,
        root_category=ROOT_CONTRACT,
        path=f"deterministic_checks[{index}]",
        observed=observed,
        expected="strict deterministic_checks row schema",
        evidence=f"deterministic_checks[{index}]: {detail}",
    )


def _validate_tolerance(value: Any, index: int, *, required: bool) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"tolerance must be a nonnegative finite number")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("tolerance must be a nonnegative finite number")
    return number


def _require_nonblank_str(value: Any, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value


def _ledger_row_failures(
    index: int, row: Any, root: dict
) -> tuple[list[GateFailure], str, str]:
    """Validate one row's contract; returns (failures, check_id, severity)."""
    if not isinstance(row, dict):
        raise ValueError("row must be an object")
    check_id = _require_nonblank_str(row.get("check_id"), "check_id", index)
    kind = row.get("kind")
    if kind not in _LEDGER_KINDS:
        raise ValueError(f"unknown check kind {kind!r}")
    path = _require_nonblank_str(row.get("path"), "path", index)
    severity = row.get("severity")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {list(SEVERITIES)}")
    _require_nonblank_str(row.get("rationale"), "rationale", index)

    if kind == "arithmetic_close":
        allowed_keys = _LEDGER_COMMON_KEYS | _LEDGER_ARITHMETIC_KEYS
    elif kind == "nonblank":
        # nonblank carries only the common keys; expected/tolerance are extras.
        allowed_keys = _LEDGER_COMMON_KEYS
    elif kind == "number_close":
        # tolerance is optional for number_close (absent => exact match):
        # allowed = common + expected, plus tolerance only when supplied.
        allowed_keys = _LEDGER_COMMON_KEYS | _LEDGER_EXPECTED_KEYS
        if "tolerance" in row:
            allowed_keys = allowed_keys | _LEDGER_TOLERANCE_KEYS
    else:
        # equals/contains/not_contains: common + expected, no tolerance key.
        allowed_keys = _LEDGER_COMMON_KEYS | _LEDGER_EXPECTED_KEYS
    actual_keys = frozenset(row)
    if actual_keys != allowed_keys:
        missing = sorted(allowed_keys - actual_keys)
        extra = sorted(actual_keys - allowed_keys)
        raise ValueError(f"exact key set violated (missing={missing}, unexpected={extra})")

    failures: list[GateFailure] = []

    def check_failed(detail: str, observed: Any, expected: Any) -> None:
        failures.append(
            _fail(
                code="deterministic_check_failed",
                severity=severity,
                root_category=ROOT_SOURCE_DATA,
                path=path,
                observed=observed,
                expected=expected,
                evidence=f"deterministic_checks[{index}] {check_id} ({kind}): {detail}",
            )
        )

    if kind == "arithmetic_close":
        scale_value = row.get("scale")
        if isinstance(scale_value, bool) or not isinstance(scale_value, (int, float)):
            raise ValueError("scale must be a finite nonzero number")
        scale = Decimal(str(scale_value))
        if not scale.is_finite() or scale == 0:
            raise ValueError("scale must be a finite nonzero number")
        tolerance = _validate_tolerance(row.get("tolerance"), index, required=True)
        if tolerance is None:
            raise ValueError("arithmetic_close requires tolerance")
        numerator, numerator_ok = _resolve_path(root, row["numerator_path"])
        denominator, denominator_ok = _resolve_path(root, row["denominator_path"])
        expected_value, expected_ok = _resolve_path(root, row["expected_path"])
        numerator_decimal = _to_decimal(numerator) if numerator_ok else None
        denominator_decimal = _to_decimal(denominator) if denominator_ok else None
        expected_decimal = _to_decimal(expected_value) if expected_ok else None
        if numerator_decimal is None:
            check_failed(
                "numerator missing, nonnumeric, or nonfinite",
                _bound(numerator) if numerator_ok else "missing",
                f"value at {row['numerator_path']}",
            )
        elif denominator_decimal is None:
            check_failed(
                "denominator missing, nonnumeric, or nonfinite",
                _bound(denominator) if denominator_ok else "missing",
                f"value at {row['denominator_path']}",
            )
        elif denominator_decimal == 0:
            check_failed(
                "division_by_zero",
                {"numerator": _bound(numerator), "denominator": _bound(denominator)},
                "nonzero denominator",
            )
        elif expected_decimal is None:
            check_failed(
                "expected value missing, nonnumeric, or nonfinite",
                _bound(expected_value) if expected_ok else "missing",
                f"value at {row['expected_path']}",
            )
        else:
            quotient = numerator_decimal / denominator_decimal * scale
            if abs(quotient - expected_decimal) > tolerance:
                check_failed(
                    f"computed {quotient} outside tolerance {tolerance} of expected",
                    _bound(expected_value),
                    f"|{row['numerator_path']} / {row['denominator_path']} * scale - "
                    f"expected| <= {tolerance}",
                )
        return failures, check_id, severity

    expected = row.get("expected")
    tolerance = None
    if kind == "number_close":
        # Optional nonnegative finite tolerance; absent means exact match.
        tolerance = _validate_tolerance(row.get("tolerance"), index, required=False)

    if kind in ("contains", "not_contains"):
        if not isinstance(expected, str) or not expected:
            raise ValueError(f"{kind} expected must be a nonblank string")
    elif kind == "number_close":
        if _to_decimal(expected) is None:
            raise ValueError("number_close expected must be numeric")

    observed, resolved = _resolve_path(root, path)
    if kind == "equals":
        # Exact structural equality; booleans never equal 1/0. No tolerance:
        # the key set above already rejects any tolerance key on this kind.
        observed_matches = resolved and (
            (isinstance(observed, bool) == isinstance(expected, bool)) and observed == expected
        )
        if not observed_matches:
            check_failed(
                "value does not equal expected",
                _bound(observed) if resolved else "missing",
                _bound(expected),
            )
    elif kind == "contains":
        if not (resolved and isinstance(observed, str) and expected in observed):
            check_failed(
                "expected text not contained in value",
                _bound(observed) if resolved else "missing",
                expected,
            )
    elif kind == "not_contains":
        if resolved and isinstance(observed, str) and expected in observed:
            check_failed(
                "forbidden text contained in value",
                _bound(observed),
                f"text absent: {expected}",
            )
    elif kind == "nonblank":
        if not (resolved and isinstance(observed, str) and observed.strip()):
            check_failed(
                "value is missing or blank",
                _bound(observed) if resolved else "missing",
                "nonblank string",
            )
    elif kind == "number_close":
        observed_decimal = _to_decimal(observed) if resolved else None
        expected_decimal = _to_decimal(expected)
        limit = tolerance if tolerance is not None else Decimal(0)
        if observed_decimal is None:
            check_failed(
                "value missing, nonnumeric, or nonfinite",
                _bound(observed) if resolved else "missing",
                f"number within {limit} of {expected}",
            )
        elif abs(observed_decimal - expected_decimal) > limit:
            check_failed(
                f"observed {observed_decimal} outside tolerance {limit} of expected",
                observed,
                f"number within {limit} of {expected}",
            )
    return failures, check_id, severity


def _ledger_failures(
    evaluator: EvaluatorCase, finalized: "InvestmentFinalizedAnalysis"
) -> list[GateFailure]:
    root = {"facts": finalized.facts, "analysis": finalized.analysis}
    failures: list[GateFailure] = []
    for index, row in enumerate(evaluator.deterministic_checks):
        try:
            row_failures, _, _ = _ledger_row_failures(index, row, root)
        except ValueError as exc:
            failures.append(_ledger_contract_failure(index, str(exc), _bound(row)))
            continue
        failures.extend(row_failures)
    return failures


def run_company_hard_gates(
    producer: ProducerCase,
    evaluator: EvaluatorCase,
    finalized: "InvestmentFinalizedAnalysis",
) -> HardGateReport:
    """Run every automatic non-compensable company-benchmark gate.

    ``finalized`` is the completed producer replay (``InvestmentFinalizedAnalysis``);
    it is consumed only after finalization. Returns a frozen report whose
    ``passed`` flag is true exactly when ``failures`` is empty. The report
    carries the producer case's SHA-256 fingerprint as its identity. Failure
    order is stable and deterministic: sorted by ``(code, path, evidence)``
    with duplicates removed.
    """
    if not isinstance(producer, ProducerCase):
        raise ValueError("run_company_hard_gates requires a ProducerCase producer")
    if not isinstance(evaluator, EvaluatorCase):
        raise ValueError("run_company_hard_gates requires an EvaluatorCase evaluator")

    failures: list[GateFailure] = []
    failures.extend(_fingerprint_failure(producer, evaluator))
    failures.extend(_production_evidence_failures(producer, finalized))
    failures.extend(_production_contract_failures(producer, finalized))
    failures.extend(_required_evidence_failures(producer, evaluator))
    failures.extend(_numeric_grounding_failures(producer, finalized))
    failures.extend(_hindsight_failures(evaluator, finalized))
    failures.extend(_prohibited_language_failures(finalized))
    failures.extend(_ledger_failures(evaluator, finalized))

    deduped: dict[tuple[str, str, str], GateFailure] = {}
    for failure in failures:
        deduped.setdefault(
            (failure.code, failure.path, failure.evidence), failure
        )
    ordered = sorted(
        deduped.values(), key=lambda item: (item.code, item.path, item.evidence)
    )
    return HardGateReport(
        passed=not ordered,
        producer_fingerprint=producer.fingerprint,
        failures=tuple(ordered),
    )


__all__ = [
    "SEVERITIES",
    "SEVERITY_CRITICAL",
    "SEVERITY_MATERIAL",
    "EvaluatorCase",
    "GateFailure",
    "HardGateReport",
    "ProducerCase",
    "run_company_hard_gates",
]
