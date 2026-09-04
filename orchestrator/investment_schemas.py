"""Pydantic v2 schemas and lightweight validation for investment analysis reports."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from processors._validators import scan_prohibited_language
from pydantic import BaseModel, ConfigDict, Field

QUALITATIVE_NAMES = (
    "ai_demand",
    "datacenter_demand",
    "supply_constraints",
    "pricing_power",
    "guidance_up",
    "guidance_down",
)
MATERIALITY_ASSESSMENT_TOPICS = (
    "forward_guidance",
    "reported_variance_driver",
    "margin_economics",
    "capital_commitment_duration",
)
NUMERIC_CLAIM_UNITS = frozenset(
    {
        "usd_billions",
        "usd_millions",
        "usd_per_share",
        "percent",
        "percentage_points",
        "basis_points",
        "count",
        "days",
        "months",
        "years",
        "ratio",
        "multiple",
        "currency",
        "shares",
    }
)
VALIDATION_JSON_SCHEMA = "json_schema"
VALIDATION_FILING_EVIDENCE = "filing_evidence"
VALIDATION_PROHIBITED_LANGUAGE = "prohibited_language"
_VALIDATION_CATEGORY_ORDER = (
    VALIDATION_JSON_SCHEMA,
    VALIDATION_FILING_EVIDENCE,
    VALIDATION_PROHIBITED_LANGUAGE,
)
_SOURCE_SPAN_HEADER_RE = re.compile(r"(?m)^\[Source characters \d+-\d+\]\n?")
_GROUNDING_TRANSLATIONS = {
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x2032: "'",
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x2033: '"',
    0x2010: "-",
    0x2011: "-",
    0x2013: "-",
    0x2014: "-",
    0x2212: "-",
    0x00A0: " ",
    0x202F: " ",
}
_MATERIAL_DISPLAYED_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9%_])(?:[+-]?[$€£¥]\s*|[$€£¥][+-]\s*|[+-]?)\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:%|bps?|basis\s+points?|trillions?|billions?|bns|bn|"
    r"millions?|mns|mn|thousands?|[tbmkx]))?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_MATERIAL_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9%_])(?:[+-]?[$€£¥]\s*|[$€£¥][+-]\s*|[+-]?)\d[\d,]*(?:\.\d+)?(?:%|bps|bp)?",
    re.IGNORECASE,
)
_MATERIAL_CALENDAR_DATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?P<iso_year>\d{4})-(?P<iso_month>\d{2})-(?P<iso_day>\d{2})|"
    r"(?P<named_month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December) (?P<named_day>\d{1,2}), (?P<named_year>\d{4})|"
    r"(?P<leading_day>\d{1,2}) (?P<leading_month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December) (?P<leading_year>\d{4})"
    r")(?![A-Za-z0-9_%])",
    re.IGNORECASE,
)
_ENGLISH_MONTH_NUMBERS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_YEAR_LIKE_TOKEN_RE = re.compile(r"(19|20)\d{2}")


class InvestmentValidationError(ValueError):
    """Exception carrying compact problem strings for model report repair."""

    def __init__(
        self,
        category_or_problems: str | list[str],
        problems: list[str] | None = None,
        *,
        category: str = VALIDATION_JSON_SCHEMA,
        problems_by_category: dict[str, list[str]] | None = None,
        **kwargs: Any,
    ):
        if isinstance(category_or_problems, str):
            self.category = category_or_problems
            self.problems = (
                list(problems) if problems is not None else [category_or_problems]
            )
        else:
            self.category = category
            self.problems = list(category_or_problems)
        self.categories = [self.category]
        self.problems_by_category = problems_by_category or {
            self.category: self.problems
        }
        super().__init__(
            f"Investment response validation failed: {'; '.join(self.problems[:3])}"
        )

    @property
    def correction_requirement(self) -> str:
        return "\n".join(f"- {p}" for p in self.problems)


def _normalize_grounding_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return (
        re.sub(r"\s+", " ", value.translate(_GROUNDING_TRANSLATIONS)).strip().casefold()
    )


def _clean_claim_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def filing_content_spans(excerpt: str) -> list[str]:
    text = excerpt if isinstance(excerpt, str) else ""
    headers = list(_SOURCE_SPAN_HEADER_RE.finditer(text))
    if not headers:
        return [text]
    spans: list[str] = []
    for index, header in enumerate(headers):
        start = header.end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[start:end]
        if block.strip():
            spans.append(block)
    return spans


def _canonical_claim_number(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return Decimal(str(value))
    if not isinstance(value, str):
        return None
    text = value.strip().casefold().replace(",", "")
    curr_tokens = re.findall(r"[$€£¥]|usd", text)
    if len(curr_tokens) > 1:
        return None
    if curr_tokens:
        text = re.sub(r"[$€£¥]|usd", "", text).strip()
    scale = Decimal(1)
    if text.endswith("bps") or text.endswith("basis points"):
        scale = Decimal("0.0001")
        text = text.removesuffix("basis points").removesuffix("bps").strip()
    elif text.endswith("bp"):
        scale = Decimal("0.0001")
        text = text.removesuffix("bp").strip()
    elif text.endswith("%"):
        scale = Decimal("0.01")
        text = text.removesuffix("%").strip()
    match = re.fullmatch(
        r"([-+]?\d+(?:\.\d+)?)\s*(trillion|billion|bns|bn|million|mns|mn|thousand|b|m|k|x)?",
        text,
    )
    if not match:
        return None
    num = Decimal(match.group(1))
    sfx = match.group(2)
    if sfx in ("trillion",):
        scale *= Decimal("1e12")
    elif sfx in ("billion", "bns", "bn", "b"):
        scale *= Decimal("1e9")
    elif sfx in ("million", "mns", "mn", "m"):
        scale *= Decimal("1e6")
    elif sfx in ("thousand", "k"):
        scale *= Decimal("1e3")
    return num * scale


def _numeric_claim_coefficient_key(value: object, unit: str = "") -> str | None:
    num = _canonical_claim_number(value)
    if num is None:
        return None
    norm = num.normalize()
    return "0" if norm == Decimal(0) else format(norm, "f")


def material_numeric_tokens(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int]] = []
    for match in _MATERIAL_CALENDAR_DATE_RE.finditer(text):
        try:
            if match.group("iso_year"):
                date(
                    int(match.group("iso_year")),
                    int(match.group("iso_month")),
                    int(match.group("iso_day")),
                )
            elif match.group("named_month"):
                date(
                    int(match.group("named_year")),
                    _ENGLISH_MONTH_NUMBERS[match.group("named_month").casefold()],
                    int(match.group("named_day")),
                )
            else:
                date(
                    int(match.group("leading_year")),
                    _ENGLISH_MONTH_NUMBERS[match.group("leading_month").casefold()],
                    int(match.group("leading_day")),
                )
            spans.append(match.span())
        except ValueError:
            continue
    found: list[tuple[int, int, str]] = []
    for match in _MATERIAL_NUMERIC_TOKEN_RE.finditer(text):
        start, end = match.span()
        if any(ds <= start and end <= de for ds, de in spans):
            continue
        if start > 0 and text[start - 1].isalnum():
            continue
        if end < len(text) and text[end].isalpha():
            disp = _MATERIAL_DISPLAYED_NUMBER_RE.match(text, start)
            if disp is None or disp.end() <= end:
                continue
        raw = match.group(0)
        key = _numeric_claim_coefficient_key(raw)
        if key is None or (
            _YEAR_LIKE_TOKEN_RE.fullmatch(raw.removesuffix(","))
            and not any(c in raw for c in "$€£¥%")
        ):
            continue
        found.append((start, end, key))
    return found


def validate_numeric_claim_rows(claims: list[dict] | None) -> list[str]:
    if claims is None:
        return []
    if not isinstance(claims, list):
        return ["$.numeric_claims: must be an array"]
    problems: list[str] = []
    if len(claims) > 40:
        problems.append("$.numeric_claims: must contain at most 40 items")
    seen_ids: set[str] = set()
    for i, row in enumerate(claims):
        if not isinstance(row, dict):
            problems.append(f"$.numeric_claims[{i}]: must be an object")
            continue
        cid = row.get("claim_id")
        if not isinstance(cid, str) or not cid.strip():
            problems.append(
                f"$.numeric_claims[{i}].claim_id: must be a nonblank string"
            )
        elif cid in seen_ids:
            problems.append(
                f"$.numeric_claims[{i}].claim_id: duplicate claim_id '{cid}'"
            )
        else:
            seen_ids.add(cid)
        val = row.get("value")
        if val is None or _canonical_claim_number(val) is None:
            problems.append(
                f"$.numeric_claims[{i}].value: must be a finite number or valid numeric string"
            )
        if (
            not isinstance(row.get("metric"), str)
            or not str(row.get("metric", "")).strip()
        ):
            problems.append(f"$.numeric_claims[{i}].metric: must be a nonblank string")
        if (
            not isinstance(row.get("period"), str)
            or not str(row.get("period", "")).strip()
        ):
            problems.append(f"$.numeric_claims[{i}].period: must be a nonblank string")
        unit, curr = row.get("unit"), row.get("currency")
        if not (
            unit in NUMERIC_CLAIM_UNITS or (isinstance(curr, str) and curr.strip())
        ):
            problems.append(
                f"$.numeric_claims[{i}]: must have a valid unit or currency"
            )
    return problems


def investment_evidence_violations(
    parsed: dict, *, excerpt: str, news_items: object = None
) -> list[str]:
    violations: list[str] = []
    spans = [_normalize_grounding_text(span) for span in filing_content_spans(excerpt)]

    def check(evidence: object, label: str, *, required: bool) -> None:
        norm = _normalize_grounding_text(evidence)
        if not norm:
            if required:
                violations.append(f"{label}: evidence is required and must be nonblank")
            return
        if not any(norm in span for span in spans):
            violations.append(
                f"{label}: evidence is not grounded in the filing excerpt"
            )

    qualitative = (
        parsed.get("qualitative") if isinstance(parsed.get("qualitative"), dict) else {}
    )
    for name in QUALITATIVE_NAMES:
        item = qualitative.get(name)
        if isinstance(item, dict):
            check(
                item.get("evidence"),
                f"qualitative.{name}",
                required=bool(item.get("present")),
            )
    assessment = (
        parsed.get("materiality_assessment")
        if isinstance(parsed.get("materiality_assessment"), dict)
        else {}
    )
    for topic in MATERIALITY_ASSESSMENT_TOPICS:
        item = assessment.get(topic)
        if isinstance(item, dict):
            check(
                item.get("evidence"),
                f"materiality_assessment.{topic}",
                required=item.get("status") == "addressed",
            )
    for coll in ("catalysts", "risks"):
        items = parsed.get(coll)
        if isinstance(items, list):
            for i, it in enumerate(items):
                if isinstance(it, dict):
                    check(it.get("evidence"), f"{coll}[{i}]", required=True)
    return violations


def validate_investment_report_payload(
    facts: dict, *, excerpt: str | None = None
) -> list[str]:
    if not isinstance(facts, dict):
        return ["$: must be an object"]
    try:
        InvestmentReport.model_validate(facts)
    except Exception as exc:
        problems = [f"$: schema validation error: {exc}"]
        return problems
    problems: list[str] = []
    if excerpt:
        problems.extend(investment_evidence_violations(facts, excerpt=excerpt))
    if facts.get("numeric_claims"):
        problems.extend(validate_numeric_claim_rows(facts["numeric_claims"]))
    prohibited = scan_prohibited_language(facts)
    if prohibited:
        problems.extend(prohibited)
    return problems


class ClassificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_type: str = Field(min_length=1, max_length=2000)
    sector: str = Field(min_length=1, max_length=2000)
    industry: str = Field(min_length=1, max_length=2000)
    region: str = Field(min_length=1, max_length=2000)
    confidence: Literal["low", "moderate", "high"]


class QualitativeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    present: bool
    strength: Literal["none", "weak", "moderate", "strong"]
    evidence: str = Field(default="", max_length=2000)


class MaterialityTopicItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["addressed", "not_disclosed"]
    observation: str = Field(default="", max_length=2000)
    implication: str = Field(default="", max_length=2000)
    evidence: str = Field(default="", max_length=2000)


class CatalystItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: str = Field(min_length=1, max_length=2000)
    expected_outcome: str = Field(min_length=1, max_length=2000)
    horizon: str = Field(min_length=1, max_length=2000)
    epistemic_state: Literal["observed", "supported", "hypothesis"]
    uncertainty: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)


class RiskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sourced_observation: str = Field(min_length=1, max_length=2000)
    inference: str = Field(min_length=1, max_length=2000)
    epistemic_state: Literal["observed", "supported", "hypothesis"]
    uncertainty: str = Field(min_length=1, max_length=2000)
    likelihood: Literal["low", "medium", "high"]
    impact: Literal["low", "medium", "high"]
    mitigation: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)


class RelationshipReconciliationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationship_id: str = Field(min_length=1, max_length=200)
    status: Literal["reconciled", "abstained_incompatible"]
    fact_paths: list[str] = Field(default_factory=list, max_length=40)
    observation: str = Field(min_length=1, max_length=2000)
    interpretation: str = Field(default="", max_length=2000)
    uncertainty: str = Field(default="", max_length=2000)
    summary_synthesis: str = Field(default="", max_length=2000)
    thesis_synthesis: str = Field(default="", max_length=2000)
    summary_fact_paths: list[str] = Field(default_factory=list, max_length=40)


class NumericClaimItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(max_length=200)
    path: str = Field(max_length=500)
    value: float | int | str
    metric: str = Field(max_length=200)
    period: str = Field(max_length=200)
    unit: str = Field(max_length=50)
    currency: str | None = Field(default=None, max_length=20)
    source_kind: Literal["text", "fact", "arithmetic"] = "text"
    quote: str | None = Field(default=None, max_length=2000)
    fact_path: str | None = Field(default=None, max_length=500)
    operation: str | None = None
    operands: list[str] | None = None


class InvestmentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: ClassificationModel
    qualitative: dict[str, QualitativeItem]
    summary: str = Field(min_length=1, max_length=2000)
    thesis: str = Field(min_length=1, max_length=2000)
    counter_thesis: str = Field(min_length=1, max_length=2000)
    materiality_assessment: dict[str, MaterialityTopicItem]
    drivers: list[str] = Field(default_factory=list, max_length=40)
    catalysts: list[CatalystItem] = Field(default_factory=list, max_length=40)
    risks: list[RiskItem] = Field(default_factory=list, max_length=40)
    watch_items: list[str] = Field(default_factory=list, max_length=40)
    relationship_reconciliations: list[RelationshipReconciliationItem] = Field(
        default_factory=list, max_length=40
    )
    numeric_claims: list[NumericClaimItem] = Field(default_factory=list, max_length=40)


def validate_risk_catalyst_contract_violations(payload: dict) -> list[str]:
    problems: list[str] = []
    risks = payload.get("risks")
    if isinstance(risks, list):
        for i, r in enumerate(risks):
            if isinstance(r, dict):
                obs = _normalize_grounding_text(r.get("sourced_observation"))
                inf = _normalize_grounding_text(r.get("inference"))
                if obs and inf and obs == inf:
                    problems.append(
                        f"$.risks[{i}]: sourced_observation and inference must differ"
                    )
    catalysts = payload.get("catalysts")
    if isinstance(catalysts, list):
        for i, c in enumerate(catalysts):
            if isinstance(c, dict):
                trig = _normalize_grounding_text(c.get("trigger"))
                exp = _normalize_grounding_text(c.get("expected_outcome"))
                if trig and exp and trig == exp:
                    problems.append(
                        f"$.catalysts[{i}]: trigger and expected_outcome must differ"
                    )
    return problems


def validate_relationship_reconciliations(
    payload: dict,
    material_relationships: tuple | list | None = None,
) -> list[str]:
    if not material_relationships:
        return []
    reconciliations = payload.get("relationship_reconciliations")
    if not isinstance(reconciliations, list) or len(reconciliations) != len(
        material_relationships
    ):
        return [
            f"relationship_reconciliations: expected exactly {len(material_relationships)} ordered rows from the request contract"
        ]
    problems: list[str] = []
    summary_text = _normalize_grounding_text(payload.get("summary"))
    thesis_text = _normalize_grounding_text(payload.get("thesis"))
    for index, (expected_rel, row) in enumerate(
        zip(material_relationships, reconciliations, strict=False)
    ):
        if not isinstance(row, dict):
            problems.append(f"relationship_reconciliations[{index}]: must be an object")
            continue
        rel_id = (
            expected_rel.get("relationship_id")
            if isinstance(expected_rel, Mapping)
            else getattr(expected_rel, "relationship_id", None)
        )
        expected_compat = (
            expected_rel.get("compatibility")
            if isinstance(expected_rel, Mapping)
            else getattr(expected_rel, "compatibility", None)
        )
        expected_paths = (
            expected_rel.get("fact_paths")
            if isinstance(expected_rel, Mapping)
            else getattr(expected_rel, "fact_paths", None)
        )
        if (
            expected_paths is None
            and isinstance(expected_rel, Mapping)
            and "required_facts" in expected_rel
        ):
            expected_paths = tuple(
                rf.get("fact_path")
                if isinstance(rf, Mapping)
                else getattr(rf, "fact_path", str(rf))
                for rf in expected_rel["required_facts"]
            )
        elif expected_paths is None and hasattr(expected_rel, "required_facts"):
            expected_paths = tuple(
                rf.get("fact_path")
                if isinstance(rf, Mapping)
                else getattr(rf, "fact_path", str(rf))
                for rf in getattr(expected_rel, "required_facts", ())
            )
        if expected_paths is None:
            expected_paths = ()
        elif not isinstance(expected_paths, (tuple, list)):
            expected_paths = tuple(expected_paths)
        if row.get("relationship_id") != rel_id:
            problems.append(
                f"relationship_reconciliations[{index}].relationship_id: must equal request relationship {rel_id!r} at this position"
            )
        if tuple(row.get("fact_paths") or ()) != tuple(expected_paths):
            problems.append(
                f"relationship_reconciliations[{index}].fact_paths: must equal the complete ordered request fact path list"
            )
        status = row.get("status")
        if expected_compat == "compatible" and status != "reconciled":
            problems.append(
                f"relationship_reconciliations[{index}].status: must be 'reconciled' for request compatibility 'compatible'"
            )
        if expected_compat == "incompatible":
            if row.get("summary_synthesis"):
                problems.append(
                    f"relationship_reconciliations[{index}].summary_synthesis: must be empty for incompatible relationship"
                )
            if row.get("thesis_synthesis"):
                problems.append(
                    f"relationship_reconciliations[{index}].thesis_synthesis: must be empty for incompatible relationship"
                )
            if row.get("summary_fact_paths"):
                problems.append(
                    f"relationship_reconciliations[{index}].summary_fact_paths: must be empty for incompatible relationship"
                )
        if status == "reconciled":
            s_synth = row.get("summary_synthesis") or ""
            t_synth = row.get("thesis_synthesis") or ""
            s_paths = row.get("summary_fact_paths") or []
            if (
                not (1 <= len(s_paths) <= 2)
                or len(set(s_paths)) != len(s_paths)
                or any(p not in expected_paths for p in s_paths)
            ):
                problems.append(
                    f"relationship_reconciliations[{index}].summary_fact_paths: must be unique 1-2 subset of fact_paths"
                )
            if s_synth and _normalize_grounding_text(s_synth) not in summary_text:
                problems.append(
                    f"relationship_reconciliations[{index}].summary_synthesis: must appear in summary"
                )
            if t_synth and _normalize_grounding_text(t_synth) not in thesis_text:
                problems.append(
                    f"relationship_reconciliations[{index}].thesis_synthesis: must appear in thesis"
                )
    return problems


INVESTMENT_REPORT_JSON_SCHEMA: dict[str, Any] = InvestmentReport.model_json_schema()
