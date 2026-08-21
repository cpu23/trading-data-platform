"""Bounded autonomous thesis-tournament engine.

This module runs a deterministic tournament over candidate theses for one
theme. It owns no SQL, no I/O, and no model calls: model calls are injected
through the :class:`RoleRunner` protocol, every candidate is validated
against the supplied evidence catalog and a strict output contract, and all
scoring math is delegated to ``thesis_scoring`` (scenario expected value and
opportunity gates) on top of the ``thesis_fusion`` canonical identity
contract.

Pipeline
--------
1. **Enumerate** — each configured role runner receives a production prompt
   plus the strict output schema and returns a JSON array of raw candidate
   objects. Raw candidates are bounded (``MAX_RAW_CANDIDATES`` total,
   ``MAX_PER_ROLE`` per role); anything beyond a bound is rejected with a
   diagnostic, never processed.
2. **Validate** — every raw candidate must match the exact output contract:
   atomic claim, subject/instrument, direction, horizon, consensus, variant
   perception, mechanism, catalyst, bull/base/bear scenario legs with a
   bounded nonblank path/assumptions description and nullable probability,
   explicit invalidators and missing evidence, and evidence refs drawn
   *only* from the supplied catalog. Unknown citations,
   embedded evidence references in prose, prohibited trade language, numbers
   not present in the supplied evidence, and ungrounded subject/instrument
   entities (no meaningful normalized entity match against the cited
   evidence, unless the explicit tournament subject matches exactly) reject
   the candidate. A runner can therefore never inject unsupported evidence.
3. **Compact** — candidates sharing the canonical thesis key (theme, subject,
   direction, horizon, mechanism) are merged for organization: narrative
   fields come from the most complete member and cited refs are unioned and
   deduplicated. Repeated agreement between roles never adds evidence mass:
   an evidence item cited by N roles is still one item, and the merged
   candidate's evidence set is the union of *distinct* refs. Opposing
   directions are always distinct keys and are preserved.
4. **Audit** — an independent citation audit re-checks every merged
   candidate's refs against the catalog (existence and point-in-time
   safety). The audit is a pure function of the drafts and the catalog; it
   never depends on which role produced what. When a
   :class:`SemanticCitationAuditor` is injected, it additionally gates
   promotion on strict per-candidate entailment decisions: the auditor
   receives the compacted candidates and the catalog, and the engine
   validates every decision exactly (keys, verdict vocabulary, cited refs
   subset, bounded fields); only ``entailed`` verdicts promote, while
   mixed/unsupported/contradicted, missing, malformed, or failed decisions
   reject explicitly. The auditor must be independent of the role runner.
5. **Promote** — only complete, evidence-cited candidates promote: every
   narrative field non-blank, all three scenario legs present (each with a
   nonblank bounded path/assumptions description), at least one explicit
   invalidator, and at least one valid citation. Incomplete competitors are
   rejected with diagnostics.
6. **Rank** — promotion-eligible candidates are ranked deterministically by
   evidence coverage (unique cited refs), independent origins (distinct
   source names among cited refs), and record completeness. Model confidence
   is never the ranking driver; it is at most a deterministic tie-break,
   followed by the canonical key, so ordering survives input permutations.
7. **Evaluate** — each ranked candidate is valued with
   ``thesis_scoring.scenario_valuation`` and gated with
   ``thesis_scoring.assess_opportunity`` exactly like the desk's
   ``evaluate_thesis`` wiring.

Missing probability/value stays unknown (null), never invented; scenario
probabilities that do not sum to one are reported, never renormalized.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from processors._validators import scan_prohibited_language
from research_intelligence.contracts import (
    EvidenceScore,
    EvidenceSignal,
    Horizon,
    NormalizedEvidence,
    OpportunityScore,
    Scenario,
    canonical_fingerprint,
    evidence_catalog,
    reject_embedded_evidence_references,
    validate_evidence_references,
)
from research_intelligence.discovery import reject_unsupported_numeric_text
from thesis_fusion import DIRECTIONS, canonical_thesis_key
from thesis_scoring import (
    CatalystSignal,
    ScenarioValuation,
    assess_evidence,
    assess_opportunity,
    calculate_neglect,
    catalyst_readiness,
    evidence_quality_prior,
    scenario_valuation,
)

# Tournament roles, in deterministic pipeline order.
ROLES = (
    "evidence_extractor",
    "fundamental",
    "expectations_revisions",
    "macro_regime",
    "supply_chain",
    "flow_options_positioning",
    "contrarian",
    "editor",
)

# Bounded pipeline inputs.
MAX_RAW_CANDIDATES = 256
MAX_PER_ROLE = 32
MAX_PROMOTED = 64
MAX_SUPPLIED_EVIDENCE = 512
MAX_EVIDENCE_REFS = 30
MAX_INVALIDATORS = 20
MAX_MISSING_EVIDENCE = 20
MAX_SEMANTIC_AUDIT_BATCH = 16
MAX_PROMPT_EVIDENCE = 200
MAX_EXCERPT_CHARS = 300
MAX_CITATIONS_PER_FIELD = 10

# Candidate domain values.
SCENARIO_LABELS = ("bull", "base", "bear")
HORIZONS = frozenset(item.value for item in Horizon)
CITATION_FIELDS = (
    "claim",
    "consensus",
    "variant_perception",
    "mechanism",
    "catalyst",
    "trend",
    "valuation",
    "sentiment",
)
_CITATION_KEYS = frozenset(CITATION_FIELDS)

# Deterministic rank blend; weights sum to 1.0. Model confidence is not part
# of the blend — it may only break ties between equal scores.
RANK_WEIGHTS = MappingProxyType(
    {
        "coverage": 0.5,
        "origins": 0.3,
        "completeness": 0.2,
    }
)
COVERAGE_TARGET = 6
ORIGINS_TARGET = 3

# Strict output contract for every role.
_REQUIRED_OUTPUT_KEYS = frozenset(
    {
        "claim",
        "subject",
        "instrument",
        "direction",
        "horizon",
        "consensus",
        "variant_perception",
        "mechanism",
        "catalyst",
        "trend_context",
        "valuation_context",
        "sentiment_context",
        "citations",
        "scenarios",
        "invalidators",
        "missing_evidence",
        "evidence_refs",
    }
)
_OPTIONAL_OUTPUT_KEYS = frozenset({"confidence"})
_SCENARIO_KEYS = frozenset({"probability", "expected_return", "description"})
#: Bounded path/assumptions description carried by every scenario leg;
#: matches the persistence bound in ``upsert_scenario``.
_SCENARIO_DESCRIPTION_MAX = 2000
_FIELD_MAX = MappingProxyType(
    {
        "claim": 5000,
        "subject": 240,
        "instrument": 240,
        "consensus": 2000,
        "variant_perception": 2000,
        "mechanism": 1000,
        "catalyst": 2000,
        "trend_context": 2000,
        "valuation_context": 2000,
        "sentiment_context": 2000,
    }
)
_LIST_ITEM_MAX = 500
_REF_MAX = 240
_TOTAL_COMPLETENESS_COMPONENTS = 16  # 12 narrative + refs + citations + lists

_COMPACT_NOTE = (
    "compacted duplicate candidate; repeated role agreement adds no evidence"
)

# Deterministic entity grounding: a candidate's subject/instrument must
# relate to the cited evidence through meaningful normalized entity keys
# before it can promote. Conservative and model-free; 1-char and generic
# tokens never ground.
GROUNDING_ENTITY_TYPES = frozenset(
    {"company", "symbol", "security", "market", "macro_region", "concept"}
)
GENERIC_ENTITY_KEYS = frozenset(
    {
        "market",
        "markets",
        "economy",
        "global",
        "world",
        "industry",
        "industries",
        "sector",
        "sectors",
        "company",
        "companies",
        "business",
        "businesses",
        "stock",
        "stocks",
        "equity",
        "equities",
        "asset",
        "assets",
        "security",
        "securities",
        "index",
        "indices",
        "the",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PAREN_SUFFIX_RE = re.compile(r"\s*\([^()]{1,20}\)\s*$")

# Optional semantic citation audit contract.
MAX_UNSUPPORTED_CLAIMS = 10
MAX_AUDIT_RATIONALE = 2000
_AUDIT_DECISION_KEYS = frozenset(
    {
        "candidate_key",
        "verdict",
        "cited_refs",
        "unsupported_claims",
        "rationale",
    }
)


class RoleRunner(Protocol):
    """Protocol for injectable model calls.

    Implementations receive the role key, the assembled production prompt,
    and the strict output schema, and return the raw model payload: a JSON
    array of candidate objects (or an empty array when the role abstains).
    The engine validates every returned object against the strict contract
    and the supplied evidence catalog, so a runner can never inject
    evidence, citations, or candidates that bypass validation.
    """

    def run(
        self,
        *,
        role: str,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> Any: ...


class CitationVerdict(StrEnum):
    """Strict entailment verdict of the semantic citation auditor."""

    ENTAILED = "entailed"
    MIXED = "mixed"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"


class SemanticCitationAuditor(Protocol):
    """Optional entailment auditor injected into ``run_tournament``.

    Implementations receive the compacted, validated candidates (as
    ``CandidateDraft.to_dict()`` payloads) plus the supplied evidence catalog
    and return a sequence of strict decision mappings — one per
    ``candidate_key`` — with exactly the keys validated by
    :func:`validate_audit_decision`. The auditor is an independent stage: it
    must not be the role runner, must not consume role outputs beyond the
    candidate payloads, must not introduce new evidence or numbers, and the
    engine promotes only ``entailed`` verdicts. Malformed, unknown, missing,
    or failed decisions reject the affected candidates explicitly.
    """

    def audit(
        self,
        *,
        candidates: Sequence[Any],
        evidence: Mapping[str, NormalizedEvidence],
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class CitationAuditDecision:
    """One strict, validated decision of the semantic citation auditor."""

    candidate_key: str
    verdict: CitationVerdict
    cited_refs: tuple[str, ...]
    unsupported_claims: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "verdict": self.verdict.value,
            "cited_refs": list(self.cited_refs),
            "unsupported_claims": list(self.unsupported_claims),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    """One validated thesis candidate after enumeration and compaction."""

    role: str
    index: int
    claim: str
    subject: str
    instrument: str
    direction: str
    horizon: str
    consensus: str
    variant_perception: str
    mechanism: str
    catalyst: str
    scenarios: tuple[Scenario, ...]
    invalidators: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    confidence: float | None
    candidate_key: str
    content_fingerprint: str
    completeness: float
    #: Bounded nonblank path/assumptions descriptions, parallel to
    #: ``scenarios`` in ``SCENARIO_LABELS`` order.  Every parsed candidate
    #: carries all three; the empty default only accommodates external
    #: direct constructions, and promotion fails closed on any blank or
    #: misaligned path.
    scenario_paths: tuple[str, ...] = ()
    trend_context: str = ""
    valuation_context: str = ""
    sentiment_context: str = ""
    citations: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "index": self.index,
            "claim": self.claim,
            "subject": self.subject,
            "instrument": self.instrument,
            "direction": self.direction,
            "horizon": self.horizon,
            "consensus": self.consensus,
            "variant_perception": self.variant_perception,
            "mechanism": self.mechanism,
            "catalyst": self.catalyst,
            "trend_context": self.trend_context,
            "valuation_context": self.valuation_context,
            "sentiment_context": self.sentiment_context,
            "citations": {key: list(refs) for key, refs in self.citations},
            "scenarios": [leg.to_dict() for leg in self.scenarios],
            "scenario_paths": list(self.scenario_paths),
            "invalidators": list(self.invalidators),
            "missing_evidence": list(self.missing_evidence),
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "candidate_key": self.candidate_key,
            "content_fingerprint": self.content_fingerprint,
            "completeness": self.completeness,
        }


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """One raw or merged candidate that did not reach the ranked output."""

    role: str
    index: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "index": self.index,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    """One candidate folded into an identical-key candidate for organization."""

    role: str
    index: int
    candidate_key: str
    evidence_refs: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "index": self.index,
            "candidate_key": self.candidate_key,
            "evidence_refs": list(self.evidence_refs),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class CitationFinding:
    """Independent citation-audit finding over candidate drafts."""

    candidate_key: str
    unknown_refs: tuple[str, ...]
    unsafe_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "unknown_refs": list(self.unknown_refs),
            "unsafe_refs": list(self.unsafe_refs),
        }


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One promotion-eligible candidate with deterministic rank and scoring."""

    candidate: CandidateDraft
    rank: int
    rank_score: float
    coverage: float
    coverage_refs: tuple[str, ...]
    independent_origins: int
    evidence: EvidenceScore
    valuation: ScenarioValuation
    opportunity: OpportunityScore

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "rank": self.rank,
            "rank_score": self.rank_score,
            "coverage": self.coverage,
            "coverage_refs": list(self.coverage_refs),
            "independent_origins": self.independent_origins,
            "evidence": self.evidence.to_dict(),
            "valuation": self.valuation.to_dict(),
            "opportunity": self.opportunity.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TournamentResult:
    """Ranked promotion-eligible candidates plus rejection/compaction logs."""

    ranked: tuple[RankedCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]
    compacted: tuple[CompactionRecord, ...]
    audit_decisions: tuple[CitationAuditDecision, ...]
    raw_candidate_count: int
    supplied_evidence_count: int
    bounds: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked": [item.to_dict() for item in self.ranked],
            "rejected": [item.to_dict() for item in self.rejected],
            "compacted": [item.to_dict() for item in self.compacted],
            "audit_decisions": [item.to_dict() for item in self.audit_decisions],
            "raw_candidate_count": self.raw_candidate_count,
            "promoted_count": len(self.ranked),
            "supplied_evidence_count": self.supplied_evidence_count,
            "bounds": dict(self.bounds),
        }


# ---------------------------------------------------------------------------
# Role specs and production prompts
# ---------------------------------------------------------------------------

ROLE_SPECS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "evidence_extractor": {
            "title": "Evidence Extractor",
            "mission": (
                "Extract atomic, falsifiable claims that are directly "
                "supported by the supplied evidence. Stay faithful to source "
                "content; do not extrapolate beyond what the cited evidence "
                "states."
            ),
            "focus": (
                "Each candidate states one causal or directional proposition "
                "whose mechanism is visible in the cited evidence. Cite every "
                "supplied piece of evidence that bears on the claim; mark "
                "anything material that is not supplied as missing evidence."
            ),
            "constraint": (
                "Prefer short-horizon, low-synthesis claims. Leave "
                "fundamental, macro, and flow interpretation to the other "
                "roles; your added value is fidelity to the evidence."
            ),
        },
        "fundamental": {
            "title": "Fundamental Analyst",
            "mission": (
                "Build fundamentals-driven theses from the supplied evidence: "
                "revenue, margins, cash flow, balance sheet, unit economics, "
                "and valuation."
            ),
            "focus": (
                "Identify where fundamentals diverge from what prices already "
                "reflect. State the mechanism that connects the cited "
                "evidence to the direction, and the catalyst that would force "
                "the market to reprice."
            ),
            "constraint": (
                "Only use figures present in the supplied evidence; never "
                "import figures from memory or models. Unknown values stay "
                "unknown and belong in missing_evidence."
            ),
        },
        "expectations_revisions": {
            "title": "Expectations and Revisions Analyst",
            "mission": (
                "Produce theses driven by consensus expectations and "
                "revisions: estimates, guidance, forecast deltas, and "
                "surprise potential."
            ),
            "focus": (
                "State what the consensus currently embeds, how the supplied "
                "evidence shows that embedding to be stale or wrong, and the "
                "revision path the evidence implies."
            ),
            "constraint": (
                "Describe consensus only from the supplied evidence; never "
                "recite market folklore without a supplied source. A stale "
                "consensus without a catalyst is not a thesis."
            ),
        },
        "macro_regime": {
            "title": "Macro and Regime Analyst",
            "mission": (
                "Transmit macro regime and policy conditions (rates, "
                "inflation, liquidity, growth) into instrument-level "
                "direction."
            ),
            "focus": (
                "Trace the transmission mechanism from the macro observation "
                "to the subject, and keep second-order effects explicit."
            ),
            "constraint": (
                "Cite the macro evidence that anchors the regime call. The "
                "horizon must match the regime's expected persistence; do "
                "not claim persistence the evidence does not support."
            ),
        },
        "supply_chain": {
            "title": "Supply-Chain and Second-Order Analyst",
            "mission": (
                "Produce supply-chain and second-order theses: bottlenecks, "
                "inventory, capacity, lead times, and propagation to "
                "downstream subjects."
            ),
            "focus": (
                "Identify which node of the chain the supplied evidence "
                "describes and which downstream subjects the condition "
                "propagates to, with the mechanism of propagation."
            ),
            "constraint": (
                "Never assume propagation the evidence does not support; "
                "mark unobserved propagation steps as missing evidence "
                "rather than asserting them."
            ),
        },
        "flow_options_positioning": {
            "title": "Flow, Options, and Positioning Analyst",
            "mission": (
                "Produce theses from positioning, flows, options market "
                "structure, and crowding: who is positioned where, and what "
                "breaks the position."
            ),
            "focus": (
                "Describe the positioning the market currently embeds, the "
                "variant view on how crowded or one-sided it is, and the "
                "catalyst that would force repricing."
            ),
            "constraint": (
                "Positioning and flow claims must trace to the supplied "
                "evidence; never invent open interest, flow, or positioning "
                "figures."
            ),
        },
        "contrarian": {
            "title": "Contrarian",
            "mission": (
                "Steelman the opposite of the consensus: produce the "
                "strongest candidate the evidence permits against the "
                "prevailing view."
            ),
            "focus": (
                "Construct the highest-quality opposing candidate, not a "
                "strawman, and cite the supplied evidence that supports the "
                "opposition."
            ),
            "constraint": (
                "Opposing candidates are preserved by the tournament: a bull "
                "and a bear candidate on the same subject are distinct and "
                "must not be weakened or flattened into one."
            ),
        },
        "editor": {
            "title": "Editor",
            "mission": (
                "Consolidate the candidate set: emit the minimal complete set "
                "of distinct theses the supplied evidence supports, with "
                "crisp claims and complete fields."
            ),
            "focus": (
                "Merge redundant phrasing into one atomic claim per distinct "
                "(subject, direction, horizon, mechanism); keep competing "
                "directions separate; make sure every field of every emitted "
                "candidate is explicit."
            ),
            "constraint": (
                "You may reorganize and sharpen candidates but never add "
                "evidence: citation sets must be drawn only from the supplied "
                "refs, and repeated agreement between roles adds no evidence."
            ),
        },
    }
)


def _catalog(
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
) -> dict[str, NormalizedEvidence]:
    """Normalize supplied evidence to a ref-keyed catalog."""
    if isinstance(evidence, Mapping):
        catalog: dict[str, NormalizedEvidence] = {}
        for ref, item in evidence.items():
            if not isinstance(item, NormalizedEvidence):
                raise ValueError("supplied evidence values must be NormalizedEvidence")
            if item.ref != str(ref):
                raise ValueError(
                    "supplied evidence mapping keys must match evidence refs"
                )
            if (
                ref in catalog
                and catalog[ref].content_fingerprint != item.content_fingerprint
            ):
                raise ValueError(f"conflicting evidence identity:{ref}")
            catalog[str(ref)] = item
        return catalog
    items = list(evidence)
    for item in items:
        if not isinstance(item, NormalizedEvidence):
            raise ValueError("supplied evidence must be NormalizedEvidence")
    return evidence_catalog(items)


def _primary_entity_key(item: NormalizedEvidence) -> str:
    """Deterministic primary grounding entity for prompt grouping.

    The first entity (in entity order) whose type is a grounding type and
    whose key is meaningful; falls back to the shared no-entity bucket.
    """
    for entity in item.entities:
        if entity.entity_type in GROUNDING_ENTITY_TYPES and _meaningful_entity_key(
            entity.normalized_key
        ):
            return f"{entity.entity_type}:{entity.normalized_key}"
    return ""


def select_prompt_evidence(
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
    limit: int = MAX_PROMPT_EVIDENCE,
) -> tuple[NormalizedEvidence, ...]:
    """Deterministically select up to ``limit`` evidence items for prompts.

    Round-robins across (evidence_type, source_name, primary entity) groups —
    groups sorted by key, members sorted by newest source_timestamp then ref
    — so rare families, sources, and symbols stay represented even under a
    dominant-symbol skew. Items without a grounding entity share a
    no-entity bucket. Identical inputs in any order yield the identical
    selection.
    """
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    catalog = _catalog(evidence)
    grouped: dict[tuple[str, str, str], list[NormalizedEvidence]] = {}
    for item in catalog.values():
        key = (item.evidence_type, item.source_name, _primary_entity_key(item))
        grouped.setdefault(key, []).append(item)
    ordered_groups: list[list[NormalizedEvidence]] = []
    for group_key in sorted(grouped):
        members = grouped[group_key]
        members.sort(
            key=lambda member: (-member.source_timestamp.timestamp(), member.ref)
        )
        ordered_groups.append(members)
    selected: list[NormalizedEvidence] = []
    while len(selected) < limit:
        progressed = False
        for members in ordered_groups:
            if len(selected) >= limit:
                break
            if members:
                selected.append(members.pop(0))
                progressed = True
        if not progressed:
            break
    return tuple(selected)


def _render_evidence_brief(
    catalog: Mapping[str, NormalizedEvidence],
) -> tuple[str, int]:
    """Render a compact, bounded evidence brief for prompts."""
    selected = select_prompt_evidence(catalog)
    lines: list[str] = []
    for item in selected:
        excerpt = " ".join(str(item.bounded_excerpt or "").split())
        if excerpt:
            excerpt = excerpt[:MAX_EXCERPT_CHARS]
        stamp = item.source_timestamp.isoformat()
        line = (
            f"- {item.ref} | {item.evidence_type} | {item.source_name} | "
            f"{stamp} | {item.title}"
        )
        if excerpt:
            line += f" | {excerpt}"
        lines.append(line)
    omitted = max(0, len(catalog) - len(selected))
    return "\n".join(lines), omitted


def _shared_prompt_block(max_candidates: int, as_of: datetime | str | None) -> str:
    horizons = ", ".join(sorted(HORIZONS))
    reference = (
        as_of.isoformat() if isinstance(as_of, datetime) else str(as_of or "now")
    )
    return f"""INPUTS
- Point-in-time analysis reference: {reference}

TASK
Produce up to {max_candidates} candidate theses. Each candidate is ONE atomic
claim about the subject with a single direction, horizon, and mechanism.
Respond with a JSON array of candidate objects (an empty array when nothing
meets the bar). Every object MUST have exactly these keys:
- claim (string, <=5000): the atomic thesis statement
- subject (string, <=240): the entity the claim is about
- instrument (string, <=240): the traded instrument the claim applies to
- direction (string): one of: long, short, neutral
- horizon (string): one of: {horizons}
- consensus (string, <=2000): what the market consensus currently prices
- variant_perception (string, <=2000): how this candidate differs from consensus
- mechanism (string, <=1000): the causal chain from the cited evidence to the outcome
- catalyst (string, <=2000): the event or condition that would make the market reprice
- trend_context (string, <=2000): a quantified, dated operating or market trend
- valuation_context (string, <=2000): current public price plus a cited filing-derived valuation or earnings measure
- sentiment_context (string, <=2000): dated measured expectations, analyst revisions, positioning, short interest, or options evidence; never infer sentiment from tone alone
- citations (object): exactly claim, consensus, variant_perception, mechanism,
  catalyst, trend, valuation, and sentiment. Each value is a nonempty array
  of exact supplied evidence_ref values supporting only that named field.
- scenarios (object): exactly bull, base, and bear legs; each leg is an object
  with "description" (string <=2000, required — never blank: the bounded path
  and assumptions of that leg), "probability" (0..1 number or null) and
  "expected_return" (fractional return within +/-100; required — never null).
  Fractional units: 0.20 means +20%, -0.15 means -15%; never emit percentage points (e.g. 20)
- invalidators (array of strings, 1..20): explicit conditions that invalidate
  the thesis; at least one is required for promotion
- missing_evidence (array of strings, <=20): evidence that would materially change the call and is not supplied
- evidence_refs (array of strings, 1..30): exactly the deduplicated union of every citations array
- confidence (number 0..1 or null, optional): your calibration; null when unknown

HARD RULES (violations reject the candidate)
- Cite only evidence_ref values listed in the supplied evidence; never
  invent, embed, or paraphrase citations in prose.
- Cite at least three independent free-source families across evidence_refs.
  Valuation citations must include filing-derived valuation/earnings evidence
  and a current public-equity trend/price. Sentiment citations must use dated
  measured expectations, positioning, short-interest, or option evidence.
  Trend citations must include quantified public-equity trend evidence.
- Never include trade instructions, entry/exit levels, stops, price targets,
  position sizing, allocation, or risk/reward advice.
- Every number in narrative text must appear verbatim in the supplied evidence
  with the same scale and unit. Never round, rescale, convert units, or derive
  a new number (for example, supplied volume 5309200 cannot become 5.3092
  million). Scenario probabilities are your judgment and may be null rather
  than invented. Expected returns are explicit, bounded scenario assumptions
  required for promotion: emit a fractional return within +/-100 for every
  leg (0.20 means +20%, -0.15 means -15%; never percentage points), or do not
  emit the candidate — never output null for expected_return, and never
  present your assumption as a sourced fact.
- Every factual narrative field must be supported by its own citations entry;
  support elsewhere in evidence_refs does not rescue a field-level citation.
- A candidate is one atomic claim: identical (subject, direction, horizon,
  mechanism) is the SAME candidate; emit it once. The tournament compacts
  duplicates and repeated agreement between roles adds no evidence.
- Candidates with opposing directions on the same subject are distinct and
  must be preserved; never flatten a bull and a bear case.
- Reason point-in-time from the evidence as of its timestamp; never cite
  future knowledge.
- List invalidators and missing evidence explicitly (a candidate without at
  least one explicit invalidator cannot promote); unknown probability or
  value stays null, never fabricated.
- Every scenario leg must describe its path and assumptions in a bounded
  nonblank description; blank descriptions reject the candidate."""


def build_role_prompt(
    *,
    role: str,
    theme_id: Any,
    subject: str | None,
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
    max_candidates: int = MAX_PER_ROLE,
    as_of: datetime | str | None = None,
) -> str:
    """Assemble the production prompt for one tournament role.

    The prompt names the role, its mission and focus, the bounded evidence
    brief, and the strict output contract plus hard rules. It is fully
    deterministic for fixed inputs.
    """
    spec = ROLE_SPECS.get(role)
    if spec is None:
        raise ValueError(f"unknown tournament role:{str(role)[:64]}")
    bounded = max(1, min(MAX_PER_ROLE, int(max_candidates)))
    catalog = _catalog(evidence)
    brief, omitted = _render_evidence_brief(catalog)
    truncation = f"\n({omitted} further supplied items omitted)" if omitted else ""
    return f"""You are the {spec["title"]} in an autonomous thesis tournament for theme {theme_id}.

MISSION
{spec["mission"]}

ROLE FOCUS
{spec["focus"]}

ROLE CONSTRAINT
{spec["constraint"]}

SUPPLIED EVIDENCE ({len(catalog)} items; cite ONLY these refs, by exact evidence_ref value)
{brief}{truncation}

{_shared_prompt_block(bounded, as_of)}"""


def role_output_schema() -> dict[str, Any]:
    """Return the strict JSON output schema shared by every role."""
    narrative: dict[str, Any] = {}
    for name, maximum in _FIELD_MAX.items():
        narrative[name] = {"type": "string", "maxLength": maximum}
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(_REQUIRED_OUTPUT_KEYS),
            "properties": {
                **narrative,
                "direction": {"enum": sorted(DIRECTIONS)},
                "horizon": {"enum": sorted(HORIZONS)},
                "scenarios": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(SCENARIO_LABELS),
                    "properties": {
                        label: {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "description",
                                "probability",
                                "expected_return",
                            ],
                            "properties": {
                                "description": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": _SCENARIO_DESCRIPTION_MAX,
                                },
                                "probability": {
                                    "type": ["number", "null"],
                                    "minimum": 0.0,
                                    "maximum": 1.0,
                                },
                                "expected_return": {"type": "number"},
                            },
                        }
                        for label in SCENARIO_LABELS
                    },
                },
                "invalidators": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_INVALIDATORS,
                },
                "missing_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_MISSING_EVIDENCE,
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_EVIDENCE_REFS,
                },
                "citations": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(CITATION_FIELDS),
                    "properties": {
                        field: {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": MAX_CITATIONS_PER_FIELD,
                        }
                        for field in CITATION_FIELDS
                    },
                },
                "confidence": {
                    "type": ["number", "null"],
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
        },
    }


def _citation_map(value: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, Mapping):
        raise ValueError("citations must be an object")
    if set(value) != _CITATION_KEYS:
        raise ValueError("citations must contain exactly " + ", ".join(CITATION_FIELDS))
    output: list[tuple[str, tuple[str, ...]]] = []
    for field in CITATION_FIELDS:
        refs = _string_list(
            value.get(field),
            MAX_CITATIONS_PER_FIELD,
            f"citations.{field}",
        )
        if not refs:
            raise ValueError(f"citations.{field} must contain at least one reference")
        output.append((field, refs))
    return tuple(output)


def _citations_dict(
    citations: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, tuple[str, ...]]:
    return {field: refs for field, refs in citations}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _bound(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value: Any, maximum: int, field: str) -> str | None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    cleaned = " ".join(value.split())
    if len(cleaned) > maximum:
        raise ValueError(f"{field} exceeds maximum length")
    return cleaned or None


def _string_list(
    value: Any,
    maximum: int,
    field: str,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds maximum items")
    output: list[str] = []
    for item in value:
        cleaned = _text(item, _LIST_ITEM_MAX, f"{field} item")
        if cleaned is None:
            raise ValueError(f"{field} item is required")
        if cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def _bounded_score(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, str):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or not (0.0 <= parsed <= 1.0):
        raise ValueError(f"{name} must be within [0, 1]")
    return parsed


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _entity_token_key(value: str) -> str:
    """Normalize a candidate subject/instrument to an entity-style key.

    Mirrors the repo's entity key normalization (``normalize_entity_key``):
    alphanumeric tokens joined by hyphens, so "Example Corp" and
    "example-corp" compare equal.
    """
    return "-".join(_TOKEN_RE.findall(value))


def _meaningful_entity_key(key: str) -> bool:
    return len(key) >= 2 and key not in GENERIC_ENTITY_KEYS


def _grounding_entities(
    references: Sequence[str],
    catalog: Mapping[str, NormalizedEvidence],
) -> set[str]:
    """Collect meaningful grounding entity keys from the cited evidence."""
    keys: set[str] = set()
    for ref in references:
        for entity in catalog[ref].entities:
            if entity.entity_type in GROUNDING_ENTITY_TYPES and _meaningful_entity_key(
                entity.normalized_key
            ):
                keys.add(entity.normalized_key)
    return keys


def _ground_candidate(
    *,
    subject: str,
    instrument: str,
    evidence_refs: Sequence[str],
    catalog: Mapping[str, NormalizedEvidence],
    theme_subject: str | None,
) -> str | None:
    """Deterministic entity grounding for one candidate.

    Returns None when grounded, otherwise a bounded rejection reason.

    The candidate's subject/instrument must relate to the cited evidence
    through at least one meaningful exact or contained normalized entity
    match (company/symbol/security/market/macro_region/concept; 1-char and
    generic tokens are ignored). An explicit tournament subject constraint
    satisfies grounding by exact normalized equality, which keeps
    macro/region candidates from unrelated-looking entity sets and lets
    evidence without entities promote only under an explicit subject.
    """
    subject_key = _entity_token_key(_normalized(subject))
    instrument_key = _entity_token_key(_normalized(instrument))
    if theme_subject is not None and subject_key == _entity_token_key(
        _normalized(theme_subject)
    ):
        return None
    candidate_keys = {
        key for key in (subject_key, instrument_key) if _meaningful_entity_key(key)
    }
    if not candidate_keys:
        return "ungrounded candidate entity: no meaningful subject or instrument"
    entity_keys = _grounding_entities(evidence_refs, catalog)
    if not entity_keys:
        return (
            "ungrounded candidate entity: cited evidence carries no grounding entities"
        )
    for candidate_key in candidate_keys:
        for entity_key in entity_keys:
            if (
                candidate_key == entity_key
                or candidate_key in entity_key
                or entity_key in candidate_key
            ):
                return None
    return "ungrounded candidate entity"


def resolve_evidence_market_identity(
    *,
    subject: str,
    instrument: str,
    evidence_refs: Sequence[str],
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
) -> tuple[str | None, str | None]:
    """Resolve prose fields to one evidence-owned company and market symbol.

    Model-authored prose is never itself treated as a market identifier.
    Resolution reuses the tournament's grounding normalization and considers
    only entities on the supplied cited evidence. Exact matches outrank
    contained matches; ambiguous companies or symbols remain ``None`` rather
    than being guessed.
    """
    catalog = _catalog(evidence)

    def match_rank(value: str, entity: Any) -> int | None:
        value_key = _entity_token_key(_normalized(value))
        if not _meaningful_entity_key(value_key):
            return None
        display_name = str(entity.display_name)
        entity_keys = {
            _entity_token_key(_normalized(entity.normalized_key)),
            _entity_token_key(_normalized(display_name)),
            _entity_token_key(_normalized(_PAREN_SUFFIX_RE.sub("", display_name))),
        }
        entity_keys = {key for key in entity_keys if _meaningful_entity_key(key)}
        if value_key in entity_keys:
            return 0
        if any(value_key in key or key in value_key for key in entity_keys):
            return 1
        return None

    company_matches: list[tuple[int, str, str, str]] = []
    symbol_matches: list[tuple[int, str, str, str]] = []
    symbols_by_ref: dict[str, list[tuple[str, str]]] = {}
    for ref in evidence_refs:
        item = catalog.get(ref)
        if item is None:
            continue
        for entity in item.entities:
            entity_type = str(entity.entity_type).casefold()
            key = _entity_token_key(_normalized(entity.normalized_key))
            display = " ".join(str(entity.display_name).split())
            if not _meaningful_entity_key(key) or not display:
                continue
            if entity_type == "company":
                rank = match_rank(subject, entity)
                if rank is not None:
                    company_matches.append((rank, key, display, ref))
            elif entity_type == "symbol":
                symbols_by_ref.setdefault(ref, []).append((key, display))
                rank = match_rank(instrument, entity)
                if rank is not None:
                    symbol_matches.append((rank, key, display, ref))

    def choose(
        matches: Sequence[tuple[int, str, str, str]],
    ) -> tuple[str | None, str | None]:
        if not matches:
            return None, None
        best_rank = min(item[0] for item in matches)
        best = [item for item in matches if item[0] == best_rank]
        keys = {item[1] for item in best}
        if len(keys) != 1:
            return None, None
        key = next(iter(keys))
        display = min(item[2] for item in best if item[1] == key)
        return key, display

    company_key, company = choose(company_matches)
    _, symbol = choose(symbol_matches)
    if symbol is None and company_key is not None:
        company_refs = {ref for _, key, _, ref in company_matches if key == company_key}
        associated_symbols = {
            key: display
            for ref in sorted(company_refs)
            for key, display in symbols_by_ref.get(ref, ())
        }
        if len(associated_symbols) == 1:
            symbol = next(iter(associated_symbols.values()))
    return company, symbol


def resolve_candidate_entities(
    candidate: CandidateDraft,
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
) -> tuple[str | None, str | None]:
    """Resolve a promoted candidate through its cited evidence entities."""
    return resolve_evidence_market_identity(
        subject=candidate.subject,
        instrument=candidate.instrument,
        evidence_refs=candidate.evidence_refs,
        evidence=evidence,
    )


def _parse_scenarios(
    raw: Any,
) -> tuple[tuple[Scenario, ...], tuple[str, ...]]:
    """Validate bull/base/bear legs; returns (legs, path descriptions).

    Every leg must carry a bounded nonblank ``description`` (the bounded
    path and assumptions of that leg) alongside its nullable probability
    and required expected return.  Paths are returned parallel to ``legs``
    in ``SCENARIO_LABELS`` order.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("scenarios must be an object")
    if set(raw) != set(SCENARIO_LABELS):
        raise ValueError("scenarios must contain exactly bull, base, and bear")
    legs: list[Scenario] = []
    paths: list[str] = []
    for label in SCENARIO_LABELS:
        leg = raw.get(label)
        if not isinstance(leg, Mapping):
            raise ValueError(f"scenario {label} must be an object")
        if set(leg) != _SCENARIO_KEYS:
            raise ValueError(f"scenario {label} keys are invalid")
        path = _text(
            leg.get("description"),
            _SCENARIO_DESCRIPTION_MAX,
            f"scenario {label} description",
        )
        if not path:
            raise ValueError(f"scenario {label} description is required")
        try:
            legs.append(
                Scenario.create(
                    label=label,
                    probability=leg.get("probability"),
                    expected_return=leg.get("expected_return"),
                )
            )
        except ValueError as exc:
            raise ValueError(f"scenario {label} is invalid:{exc}") from exc
        paths.append(path)
    return tuple(legs), tuple(paths)


def _completeness(
    *,
    claim: str,
    subject: str,
    instrument: str,
    direction: str,
    horizon: str,
    consensus: str,
    variant_perception: str,
    mechanism: str,
    catalyst: str,
    trend_context: str,
    valuation_context: str,
    sentiment_context: str,
    invalidators: tuple[str, ...],
    missing_evidence: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    citations: tuple[tuple[str, tuple[str, ...]], ...],
) -> float:
    """Fraction of the complete actionable-thesis contract carrying content."""
    narrative = (
        claim,
        subject,
        instrument,
        direction,
        horizon,
        consensus,
        variant_perception,
        mechanism,
        catalyst,
        trend_context,
        valuation_context,
        sentiment_context,
    )
    present = sum(1 for value in narrative if value)
    present += 1 if invalidators else 0
    present += 1 if missing_evidence else 0
    present += 1 if evidence_refs else 0
    present += 1 if citations else 0
    return present / float(_TOTAL_COMPLETENESS_COMPONENTS)


def _candidate_fingerprint(
    *,
    claim: str,
    subject: str,
    instrument: str,
    direction: str,
    horizon: str,
    consensus: str,
    variant_perception: str,
    mechanism: str,
    catalyst: str,
    trend_context: str,
    valuation_context: str,
    sentiment_context: str,
    scenarios: tuple[Scenario, ...],
    scenario_paths: tuple[str, ...],
    invalidators: tuple[str, ...],
    missing_evidence: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    citations: tuple[tuple[str, tuple[str, ...]], ...],
    confidence: float | None,
) -> str:
    return canonical_fingerprint(
        {
            "claim": claim,
            "subject": subject,
            "instrument": instrument,
            "direction": direction,
            "horizon": horizon,
            "consensus": consensus,
            "variant_perception": variant_perception,
            "mechanism": mechanism,
            "catalyst": catalyst,
            "trend_context": trend_context,
            "valuation_context": valuation_context,
            "sentiment_context": sentiment_context,
            "scenarios": [
                {
                    "label": leg.label,
                    "probability": leg.probability,
                    "expected_return": leg.expected_return,
                    "description": path,
                }
                for leg, path in zip(scenarios, scenario_paths, strict=True)
            ],
            "invalidators": invalidators,
            "missing_evidence": missing_evidence,
            "evidence_refs": evidence_refs,
            "citations": citations,
            "confidence": confidence,
        }
    )


def _parse_candidate(
    *,
    role: str,
    index: int,
    raw: Any,
    catalog: Mapping[str, NormalizedEvidence],
    theme_id: Any,
    theme_subject: str | None = None,
) -> CandidateDraft:
    """Validate one raw role output against the strict contract.

    Raises ValueError with a bounded, specific reason on any violation; the
    engine converts each into a :class:`RejectedCandidate` diagnostic.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("candidate must be an object")
    keys = set(raw)
    unexpected = keys - (_REQUIRED_OUTPUT_KEYS | _OPTIONAL_OUTPUT_KEYS)
    if unexpected:
        raise ValueError(f"unexpected keys: {', '.join(sorted(unexpected))[:120]}")
    missing = _REQUIRED_OUTPUT_KEYS - keys
    if missing:
        raise ValueError(f"missing required fields: {', '.join(sorted(missing))[:120]}")
    claim = _text(raw.get("claim"), _FIELD_MAX["claim"], "claim") or ""
    subject = _text(raw.get("subject"), _FIELD_MAX["subject"], "subject") or ""
    instrument = (
        _text(raw.get("instrument"), _FIELD_MAX["instrument"], "instrument") or ""
    )
    direction = (_text(raw.get("direction"), 40, "direction") or "").casefold()
    if direction not in DIRECTIONS:
        raise ValueError(f"invalid direction:{direction[:32]}")
    horizon = (_text(raw.get("horizon"), 40, "horizon") or "").casefold()
    if horizon not in HORIZONS:
        raise ValueError(f"invalid horizon:{horizon[:32]}")
    consensus = _text(raw.get("consensus"), _FIELD_MAX["consensus"], "consensus") or ""
    variant = (
        _text(
            raw.get("variant_perception"),
            _FIELD_MAX["variant_perception"],
            "variant_perception",
        )
        or ""
    )
    mechanism = _text(raw.get("mechanism"), _FIELD_MAX["mechanism"], "mechanism") or ""
    catalyst = _text(raw.get("catalyst"), _FIELD_MAX["catalyst"], "catalyst") or ""
    trend_context = (
        _text(raw.get("trend_context"), _FIELD_MAX["trend_context"], "trend_context")
        or ""
    )
    valuation_context = (
        _text(
            raw.get("valuation_context"),
            _FIELD_MAX["valuation_context"],
            "valuation_context",
        )
        or ""
    )
    sentiment_context = (
        _text(
            raw.get("sentiment_context"),
            _FIELD_MAX["sentiment_context"],
            "sentiment_context",
        )
        or ""
    )
    citations = _citation_map(raw.get("citations"))
    scenarios, scenario_paths = _parse_scenarios(raw.get("scenarios"))
    invalidators = _string_list(
        raw.get("invalidators"), MAX_INVALIDATORS, "invalidators"
    )
    missing_evidence = _string_list(
        raw.get("missing_evidence"), MAX_MISSING_EVIDENCE, "missing_evidence"
    )
    raw_refs = _string_list(
        raw.get("evidence_refs"), MAX_EVIDENCE_REFS, "evidence_refs"
    )
    if not raw_refs:
        raise ValueError(
            "evidence_refs must contain at least one supplied evidence reference"
        )

    # Evidence refs and the structured citation map are the only sanctioned
    # citation channels; refs embedded in narrative prose are rejected.
    narrative = {
        key: value
        for key, value in raw.items()
        if key not in {"evidence_refs", "citations"}
    }
    reject_embedded_evidence_references(narrative)
    narrative_text = {
        "claim": claim,
        "subject": subject,
        "instrument": instrument,
        "consensus": consensus,
        "variant_perception": variant,
        "mechanism": mechanism,
        "catalyst": catalyst,
        "trend_context": trend_context,
        "valuation_context": valuation_context,
        "sentiment_context": sentiment_context,
        "invalidators": invalidators,
        "missing_evidence": missing_evidence,
    }
    findings = scan_prohibited_language(narrative_text)
    if findings:
        raise ValueError(f"prohibited trade language:{findings[0][:160]}")
    try:
        reject_unsupported_numeric_text(narrative_text, list(catalog.values()))
    except ValueError as exc:
        raise ValueError(f"unsupported numeric claim:{exc}") from exc

    try:
        references = validate_evidence_references(raw_refs, catalog)
    except ValueError as exc:
        raise ValueError(f"invalid evidence citation:{exc}") from exc
    references = tuple(sorted(set(references)))
    for ref in references:
        if not catalog[ref].point_in_time_safe:
            raise ValueError(f"evidence is not point-in-time safe:{ref}")

    normalized_citations: list[tuple[str, tuple[str, ...]]] = []
    citation_union: set[str] = set()
    for field, field_refs in citations:
        try:
            validated_refs = validate_evidence_references(field_refs, catalog)
        except ValueError as exc:
            raise ValueError(f"invalid citations.{field}:{exc}") from exc
        normalized_refs = tuple(sorted(set(validated_refs)))
        normalized_citations.append((field, normalized_refs))
        citation_union.update(normalized_refs)
    citations = tuple(normalized_citations)
    if citation_union != set(references):
        raise ValueError("evidence_refs must equal the deduplicated union of citations")

    citation_values = {
        "claim": claim,
        "consensus": consensus,
        "variant_perception": variant,
        "mechanism": mechanism,
        "catalyst": catalyst,
        "trend": trend_context,
        "valuation": valuation_context,
        "sentiment": sentiment_context,
    }
    citation_lookup = _citations_dict(citations)
    for field, value in citation_values.items():
        try:
            reject_unsupported_numeric_text(
                {field: value},
                [catalog[ref] for ref in citation_lookup[field]],
            )
        except ValueError as exc:
            raise ValueError(f"unsupported numeric claim in {field}:{exc}") from exc

    # Deterministic entity grounding: the candidate's subject/instrument must
    # relate to the cited evidence (or match the explicit tournament subject).
    grounding_reason = _ground_candidate(
        subject=subject,
        instrument=instrument,
        evidence_refs=references,
        catalog=catalog,
        theme_subject=theme_subject,
    )
    if grounding_reason is not None:
        raise ValueError(grounding_reason)

    confidence = None
    if "confidence" in keys:
        confidence = _bounded_score(raw.get("confidence"), "confidence")

    candidate_key = canonical_thesis_key(
        theme_id=theme_id,
        subject=subject,
        direction=direction,
        horizon=horizon,
        mechanism=mechanism,
    )
    completeness = _completeness(
        claim=claim,
        subject=subject,
        instrument=instrument,
        direction=direction,
        horizon=horizon,
        consensus=consensus,
        variant_perception=variant,
        mechanism=mechanism,
        catalyst=catalyst,
        trend_context=trend_context,
        valuation_context=valuation_context,
        sentiment_context=sentiment_context,
        invalidators=invalidators,
        missing_evidence=missing_evidence,
        evidence_refs=references,
        citations=citations,
    )
    content_fingerprint = _candidate_fingerprint(
        claim=claim,
        subject=subject,
        instrument=instrument,
        direction=direction,
        horizon=horizon,
        consensus=consensus,
        variant_perception=variant,
        mechanism=mechanism,
        catalyst=catalyst,
        trend_context=trend_context,
        valuation_context=valuation_context,
        sentiment_context=sentiment_context,
        scenarios=scenarios,
        scenario_paths=scenario_paths,
        invalidators=invalidators,
        missing_evidence=missing_evidence,
        evidence_refs=references,
        citations=citations,
        confidence=confidence,
    )
    return CandidateDraft(
        role=role,
        index=index,
        claim=claim,
        subject=subject,
        instrument=instrument,
        direction=direction,
        horizon=horizon,
        consensus=consensus,
        variant_perception=variant,
        mechanism=mechanism,
        catalyst=catalyst,
        trend_context=trend_context,
        valuation_context=valuation_context,
        sentiment_context=sentiment_context,
        citations=citations,
        scenarios=scenarios,
        scenario_paths=scenario_paths,
        invalidators=invalidators,
        missing_evidence=missing_evidence,
        evidence_refs=references,
        confidence=confidence,
        candidate_key=candidate_key,
        content_fingerprint=content_fingerprint,
        completeness=completeness,
    )


# ---------------------------------------------------------------------------
# Compaction, audit, eligibility, ranking, evaluation
# ---------------------------------------------------------------------------


def _merge_members(key: str, members: Sequence[CandidateDraft]) -> CandidateDraft:
    """Merge same-key candidates for organization.

    The representative carries the complete narrative, citations, evidence,
    scenarios, and invalidators (ties break by confidence then content
    fingerprint). Compaction never adds a different role's evidence to the
    representative's claim: field-level citation entailment must remain
    auditable after merging. Missing-evidence requests are the only union.
    """
    representative = max(
        members,
        key=lambda draft: (
            draft.completeness,
            draft.confidence if draft.confidence is not None else 0.0,
            draft.content_fingerprint,
        ),
    )
    refs = representative.evidence_refs
    invalidators = representative.invalidators
    missing_evidence = tuple(
        sorted({item for draft in members for item in draft.missing_evidence})
    )
    completeness = _completeness(
        claim=representative.claim,
        subject=representative.subject,
        instrument=representative.instrument,
        direction=representative.direction,
        horizon=representative.horizon,
        consensus=representative.consensus,
        variant_perception=representative.variant_perception,
        mechanism=representative.mechanism,
        catalyst=representative.catalyst,
        trend_context=representative.trend_context,
        valuation_context=representative.valuation_context,
        sentiment_context=representative.sentiment_context,
        invalidators=invalidators,
        missing_evidence=missing_evidence,
        evidence_refs=refs,
        citations=representative.citations,
    )
    return CandidateDraft(
        role=representative.role,
        index=representative.index,
        claim=representative.claim,
        subject=representative.subject,
        instrument=representative.instrument,
        direction=representative.direction,
        horizon=representative.horizon,
        consensus=representative.consensus,
        variant_perception=representative.variant_perception,
        mechanism=representative.mechanism,
        catalyst=representative.catalyst,
        trend_context=representative.trend_context,
        valuation_context=representative.valuation_context,
        sentiment_context=representative.sentiment_context,
        citations=representative.citations,
        scenarios=representative.scenarios,
        scenario_paths=representative.scenario_paths,
        invalidators=invalidators,
        missing_evidence=missing_evidence,
        evidence_refs=refs,
        confidence=representative.confidence,
        candidate_key=key,
        content_fingerprint=representative.content_fingerprint,
        completeness=completeness,
    )


def audit_citations(
    candidates: Sequence[CandidateDraft],
    supplied: Mapping[str, NormalizedEvidence],
) -> tuple[CitationFinding, ...]:
    """Independent citation audit over candidate drafts.

    This is a pure function of the drafts and the supplied catalog: it never
    consumes runner outputs beyond the validated drafts, so citation audit is
    independent of the generators. Findings report refs that are unknown to
    the catalog and refs that are not point-in-time safe.
    """
    findings: list[CitationFinding] = []
    for draft in candidates:
        unknown = tuple(
            sorted(ref for ref in draft.evidence_refs if ref not in supplied)
        )
        unsafe = tuple(
            sorted(
                ref
                for ref in draft.evidence_refs
                if ref in supplied and not supplied[ref].point_in_time_safe
            )
        )
        if unknown or unsafe:
            findings.append(
                CitationFinding(
                    candidate_key=draft.candidate_key,
                    unknown_refs=unknown,
                    unsafe_refs=unsafe,
                )
            )
    return tuple(findings)


def _missing_promotion_fields(draft: CandidateDraft) -> tuple[str, ...]:
    """Narrative fields whose absence blocks promotion of a competitor.

    Promotion also fails closed when a scenario leg lacks a bounded
    nonblank path/assumptions description or no explicit invalidator was
    supplied: blank descriptions and empty invalidators must never create
    an investable row (invalidators materialize as the candidate's
    structured risks).
    """
    missing_fields: list[str] = []
    for name in (
        "claim",
        "subject",
        "instrument",
        "consensus",
        "variant_perception",
        "mechanism",
        "catalyst",
    ):
        if not getattr(draft, name):
            missing_fields.append(name)
    if len(draft.scenario_paths) != len(draft.scenarios) or any(
        not path for path in draft.scenario_paths
    ):
        missing_fields.append("scenario_paths")
    if not draft.invalidators:
        missing_fields.append("invalidators")
    if not draft.evidence_refs:
        missing_fields.append("evidence_refs")
    return tuple(missing_fields)


def _signal_for(item: NormalizedEvidence) -> EvidenceSignal:
    """Derive a desk scoring signal from one cited evidence item.

    Cited evidence is support for the candidate; same-source items share a
    source family and are correlation-capped by ``thesis_scoring``. Agent
    provenance is excluded from the signal so agreement never scores. The
    signal carries the item's bounded excerpt (plus its structured payload,
    when present) and the shared deterministic quality prior, with
    entailment 1.0: only audit-entailed candidates are ranked, so their
    cited support is fully entailed here; the desk recomputes scores at
    persistence time with the graded audit entailment.
    """
    signal_provenance: dict[str, Any] = {
        "excerpt": item.bounded_excerpt,
        "source_reference": item.source_reference,
    }
    structured = (
        item.structured_fields if isinstance(item.structured_fields, Mapping) else {}
    )
    if structured:
        signal_provenance["structured_fields"] = dict(structured)
    return EvidenceSignal.create(
        evidence_id=item.evidence_id,
        evidence_type=item.evidence_type,
        relationship="supports",
        source_name=item.source_name,
        source_family=item.source_name,
        origin_key=None,
        independence_key=None,
        evidence_fingerprint=item.content_fingerprint,
        source_timestamp=item.source_timestamp,
        available_at=item.available_at,
        quality_score=evidence_quality_prior(item),
        entailment_score=1.0,
        provenance=signal_provenance,
    )


def _rank_components(
    draft: CandidateDraft,
    catalog: Mapping[str, NormalizedEvidence],
) -> tuple[float, float, int]:
    """Deterministic rank components: coverage, independent origins, score."""
    refs = draft.evidence_refs
    coverage = min(1.0, len(refs) / float(COVERAGE_TARGET))
    origins = min(
        1.0,
        len({catalog[ref].source_name for ref in refs}) / float(ORIGINS_TARGET),
    )
    score = (
        RANK_WEIGHTS["coverage"] * coverage
        + RANK_WEIGHTS["origins"] * origins
        + RANK_WEIGHTS["completeness"] * draft.completeness
    )
    return score, coverage, len({catalog[ref].source_name for ref in refs})


def _evaluate(
    draft: CandidateDraft,
    catalog: Mapping[str, NormalizedEvidence],
    *,
    cost: Any,
    attention: Any,
    crowding: Any,
    liquidity: Any,
    downside: Any,
    as_of: datetime | str | None,
) -> tuple[EvidenceScore, ScenarioValuation, OpportunityScore]:
    """Score one candidate with the thesis-scoring foundation."""
    signals = tuple(_signal_for(catalog[ref]) for ref in draft.evidence_refs)
    evidence = assess_evidence(signals)
    neglect = calculate_neglect(attention=attention, crowding=crowding)
    catalyst = catalyst_readiness(
        [CatalystSignal.create(description=draft.catalyst, state="pending")],
        as_of=as_of,
    )
    valuation = scenario_valuation(draft.scenarios, cost=cost)
    opportunity = assess_opportunity(
        evidence_strength=evidence.support_mass,
        confidence=evidence.confidence,
        neglect=neglect.neglect,
        catalyst_ready=catalyst.readiness,
        liquidity=liquidity,
        downside=downside,
    )
    return evidence, valuation, opportunity


# ---------------------------------------------------------------------------
# Tournament entry point
# ---------------------------------------------------------------------------


def validate_audit_decision(raw: Any, draft: CandidateDraft) -> CitationAuditDecision:
    """Strictly validate one semantic-audit decision against its candidate.

    Raises ValueError on any contract violation: wrong keys, unknown
    candidate_key, invalid verdict, cited refs outside the candidate's refs,
    or over-bounded fields. The engine treats any violation as an audit
    failure for the batch.
    """
    if not isinstance(raw, Mapping) or set(raw) != _AUDIT_DECISION_KEYS:
        raise ValueError("audit decision keys do not match the strict contract")
    candidate_key = raw.get("candidate_key")
    if candidate_key != draft.candidate_key:
        raise ValueError("audit decision references an unknown candidate")
    verdict = str(raw.get("verdict") or "").strip().casefold()
    if verdict not in {item.value for item in CitationVerdict}:
        raise ValueError("audit verdict is invalid")
    refs = _string_list(raw.get("cited_refs"), MAX_EVIDENCE_REFS, "cited_refs")
    if not set(refs) <= set(draft.evidence_refs):
        raise ValueError(
            "audit cited_refs must be a subset of the candidate's evidence refs"
        )
    unsupported = _string_list(
        raw.get("unsupported_claims"),
        MAX_UNSUPPORTED_CLAIMS,
        "unsupported_claims",
    )
    if verdict == CitationVerdict.ENTAILED.value:
        if set(refs) != set(draft.evidence_refs):
            raise ValueError("entailed audit must confirm every field-level citation")
        if unsupported:
            raise ValueError("entailed audit cannot report unsupported claim parts")
    rationale = _text(raw.get("rationale"), MAX_AUDIT_RATIONALE, "rationale") or ""
    return CitationAuditDecision(
        candidate_key=candidate_key,
        verdict=CitationVerdict(verdict),
        cited_refs=refs,
        unsupported_claims=unsupported,
        rationale=rationale,
    )


def _audit_batch_failure(
    candidates: Sequence[CandidateDraft],
    detail: str,
) -> tuple[
    tuple[CitationAuditDecision, ...], list[CandidateDraft], list[RejectedCandidate]
]:
    return (
        (),
        [],
        [
            RejectedCandidate(role=draft.role, index=draft.index, reason=detail)
            for draft in candidates
        ],
    )


def _run_semantic_audit(
    auditor: SemanticCitationAuditor,
    candidates: Sequence[CandidateDraft],
    catalog: Mapping[str, NormalizedEvidence],
) -> tuple[
    tuple[CitationAuditDecision, ...], list[CandidateDraft], list[RejectedCandidate]
]:
    """Run the injected semantic auditor and strictly validate its output.

    Every decision must be well-formed, unique, and reference a known
    candidate; candidates without a decision are rejected as missing, and
    non-``entailed`` verdicts are rejected explicitly. Any exception,
    malformed decision, unknown candidate key, or out-of-contract ref set
    fails the whole batch deterministically.
    """
    try:
        output = auditor.audit(
            candidates=[draft.to_dict() for draft in candidates],
            evidence=catalog,
        )
    except Exception as exc:
        return _audit_batch_failure(
            candidates,
            f"citation audit failed:{type(exc).__name__}:{str(exc)[:200]}",
        )
    if (
        isinstance(output, Mapping)
        or not isinstance(output, Sequence)
        or isinstance(output, (str, bytes))
    ):
        return _audit_batch_failure(
            candidates,
            "citation audit failed:auditor output must be a sequence of decisions",
        )
    by_key = {draft.candidate_key: draft for draft in candidates}
    decisions_by_key: dict[str, CitationAuditDecision] = {}
    for raw in output:
        if not isinstance(raw, Mapping) or set(raw) != _AUDIT_DECISION_KEYS:
            return _audit_batch_failure(
                candidates, "citation audit failed:malformed decision"
            )
        draft = by_key.get(raw.get("candidate_key"))
        if draft is None:
            return _audit_batch_failure(
                candidates,
                "citation audit failed:decision references an unknown candidate",
            )
        try:
            decision = validate_audit_decision(raw, draft)
        except ValueError as exc:
            return _audit_batch_failure(candidates, f"citation audit failed:{exc}")
        if draft.candidate_key in decisions_by_key:
            return _audit_batch_failure(
                candidates, "citation audit failed:duplicate decision"
            )
        decisions_by_key[draft.candidate_key] = decision
    decisions: list[CitationAuditDecision] = []
    survivors: list[CandidateDraft] = []
    rejections: list[RejectedCandidate] = []
    for draft in candidates:
        decision = decisions_by_key.get(draft.candidate_key)
        if decision is None:
            rejections.append(
                RejectedCandidate(
                    role=draft.role,
                    index=draft.index,
                    reason="citation audit failed:missing decision",
                )
            )
            continue
        decisions.append(decision)
        if decision.verdict is CitationVerdict.ENTAILED:
            survivors.append(draft)
        else:
            rejections.append(
                RejectedCandidate(
                    role=draft.role,
                    index=draft.index,
                    reason=f"citation audit failed:verdict:{decision.verdict.value}",
                )
            )
    return tuple(decisions), survivors, rejections


def run_tournament(
    *,
    theme_id: Any,
    runner: RoleRunner,
    auditor: SemanticCitationAuditor | None = None,
    evidence: Sequence[NormalizedEvidence] | Mapping[str, NormalizedEvidence],
    roles: Sequence[str] = ROLES,
    subject: str | None = None,
    max_raw_candidates: int = MAX_RAW_CANDIDATES,
    max_per_role: int = MAX_PER_ROLE,
    max_promoted: int = MAX_PROMOTED,
    cost: Any = 0.0,
    attention: Any = None,
    crowding: Any = None,
    liquidity: Any = None,
    downside: Any = None,
    as_of: datetime | str | None = None,
) -> TournamentResult:
    """Run one bounded tournament and return ranked/rejected diagnostics.

    Model calls are injected through ``runner``; every returned candidate is
    validated against the strict contract and the supplied evidence catalog
    before it can influence any output. The pipeline is deterministic for
    fixed inputs (see module docstring).
    """
    catalog = _catalog(evidence)
    if len(catalog) > MAX_SUPPLIED_EVIDENCE:
        raise ValueError("supplied evidence exceeds the configured bound")
    # Validate theme identity up front (config error, not model output).
    canonical_thesis_key(
        theme_id=theme_id,
        subject="probe",
        direction="long",
        horizon="days",
        mechanism="probe",
    )
    role_list = list(roles)
    unknown_roles = sorted({role for role in role_list if role not in ROLE_SPECS})
    if unknown_roles:
        raise ValueError(f"unknown tournament roles: {', '.join(unknown_roles)[:120]}")
    if len(set(role_list)) != len(role_list):
        raise ValueError("tournament roles must be unique")

    max_raw = _bound(max_raw_candidates, MAX_RAW_CANDIDATES, MAX_RAW_CANDIDATES)
    max_role = _bound(max_per_role, MAX_PER_ROLE, MAX_PER_ROLE)
    max_prom = _bound(max_promoted, MAX_PROMOTED, MAX_PROMOTED)
    schema = role_output_schema()

    drafts: list[CandidateDraft] = []
    rejected: list[RejectedCandidate] = []
    raw_count = 0
    for role in role_list:
        prompt = build_role_prompt(
            role=role,
            theme_id=theme_id,
            subject=subject,
            evidence=list(catalog.values()),
            max_candidates=max_role,
            as_of=as_of,
        )
        try:
            output = runner.run(role=role, prompt=prompt, schema=schema)
        except Exception as exc:  # fail soft: one role never sinks the desk
            rejected.append(
                RejectedCandidate(
                    role=role,
                    index=-1,
                    reason=f"role runner failed:{type(exc).__name__}:{str(exc)[:200]}",
                )
            )
            continue
        if not isinstance(output, list):
            rejected.append(
                RejectedCandidate(
                    role=role,
                    index=-1,
                    reason="role output must be a JSON array of candidate objects",
                )
            )
            continue
        for index, raw in enumerate(output):
            if index >= max_role:
                rejected.append(
                    RejectedCandidate(
                        role=role,
                        index=index,
                        reason="per-role candidate bound exceeded",
                    )
                )
                continue
            if raw_count >= max_raw:
                rejected.append(
                    RejectedCandidate(
                        role=role,
                        index=index,
                        reason="raw candidate bound exceeded",
                    )
                )
                continue
            raw_count += 1
            try:
                drafts.append(
                    _parse_candidate(
                        role=role,
                        index=index,
                        raw=raw,
                        catalog=catalog,
                        theme_id=theme_id,
                        theme_subject=subject,
                    )
                )
            except ValueError as exc:
                rejected.append(
                    RejectedCandidate(role=role, index=index, reason=str(exc))
                )

    # Compaction: identical canonical keys merge for organization only.
    grouped: dict[str, list[CandidateDraft]] = {}
    for draft in drafts:
        grouped.setdefault(draft.candidate_key, []).append(draft)
    merged: list[CandidateDraft] = []
    compacted: list[CompactionRecord] = []
    for key, members in grouped.items():
        if len(members) > 1:
            for extra in members[1:]:
                compacted.append(
                    CompactionRecord(
                        role=extra.role,
                        index=extra.index,
                        candidate_key=key,
                        evidence_refs=extra.evidence_refs,
                        note=_COMPACT_NOTE,
                    )
                )
        merged.append(_merge_members(key, members))

    # Independent citation audit (defense in depth over parse-time checks).
    findings = audit_citations(merged, catalog)
    audited_keys = {finding.candidate_key for finding in findings}
    audited = [draft for draft in merged if draft.candidate_key not in audited_keys]
    for finding in findings:
        rejected.append(
            RejectedCandidate(
                role="audit",
                index=-1,
                reason=(
                    "citation audit failed:"
                    f"unknown={','.join(finding.unknown_refs)}"
                    f";unsafe={','.join(finding.unsafe_refs)}"
                ),
            )
        )

    # Independent semantic entailment audit. Partitioning bounds response size
    # and isolates a provider failure to its batch while every affected
    # candidate still fails closed.
    audit_decisions: tuple[CitationAuditDecision, ...] = ()
    if auditor is not None:
        if auditor is runner:
            raise ValueError("citation auditor must be independent of the role runner")
        decisions: list[CitationAuditDecision] = []
        survivors: list[CandidateDraft] = []
        for offset in range(0, len(audited), MAX_SEMANTIC_AUDIT_BATCH):
            batch = audited[offset : offset + MAX_SEMANTIC_AUDIT_BATCH]
            batch_decisions, batch_survivors, batch_rejections = _run_semantic_audit(
                auditor, batch, catalog
            )
            decisions.extend(batch_decisions)
            survivors.extend(batch_survivors)
            rejected.extend(batch_rejections)
        audit_decisions = tuple(decisions)
        audited = survivors

    # Promotion gate: only complete, evidence-cited candidates compete.
    for draft in audited:
        missing_fields = _missing_promotion_fields(draft)
        if missing_fields:
            rejected.append(
                RejectedCandidate(
                    role=draft.role,
                    index=draft.index,
                    reason=(
                        "incomplete candidate does not promote:"
                        + ",".join(missing_fields)
                    ),
                )
            )
    eligible = [draft for draft in audited if not _missing_promotion_fields(draft)]

    # Deterministic ranking: score, then confidence tie-break, then key.
    scored = [(*_rank_components(draft, catalog), draft) for draft in eligible]
    scored.sort(
        key=lambda item: (
            -item[0],
            -(item[3].confidence if item[3].confidence is not None else 0.0),
            item[3].candidate_key,
        )
    )

    ranked: list[RankedCandidate] = []
    for position, (score, coverage, origins, draft) in enumerate(scored[:max_prom]):
        evidence_score, valuation, opportunity = _evaluate(
            draft,
            catalog,
            cost=cost,
            attention=attention,
            crowding=crowding,
            liquidity=liquidity,
            downside=downside,
            as_of=as_of,
        )
        ranked.append(
            RankedCandidate(
                candidate=draft,
                rank=position + 1,
                rank_score=score,
                coverage=coverage,
                coverage_refs=draft.evidence_refs,
                independent_origins=origins,
                evidence=evidence_score,
                valuation=valuation,
                opportunity=opportunity,
            )
        )
    for _, _, _, draft in scored[max_prom:]:
        rejected.append(
            RejectedCandidate(
                role=draft.role,
                index=draft.index,
                reason="promotion bound exceeded",
            )
        )

    return TournamentResult(
        ranked=tuple(ranked),
        rejected=tuple(rejected),
        compacted=tuple(compacted),
        audit_decisions=audit_decisions,
        raw_candidate_count=raw_count,
        supplied_evidence_count=len(catalog),
        bounds=MappingProxyType(
            {
                "max_raw_candidates": max_raw,
                "max_per_role": max_role,
                "max_promoted": max_prom,
                "max_evidence_refs": MAX_EVIDENCE_REFS,
                "max_invalidators": MAX_INVALIDATORS,
                "max_missing_evidence": MAX_MISSING_EVIDENCE,
            }
        ),
    )


__all__ = [
    "COVERAGE_TARGET",
    "CandidateDraft",
    "CitationAuditDecision",
    "CitationFinding",
    "CitationVerdict",
    "CompactionRecord",
    "GENERIC_ENTITY_KEYS",
    "GROUNDING_ENTITY_TYPES",
    "HORIZONS",
    "MAX_AUDIT_RATIONALE",
    "MAX_EVIDENCE_REFS",
    "MAX_INVALIDATORS",
    "MAX_MISSING_EVIDENCE",
    "MAX_PER_ROLE",
    "MAX_PROMOTED",
    "MAX_PROMPT_EVIDENCE",
    "MAX_RAW_CANDIDATES",
    "MAX_SUPPLIED_EVIDENCE",
    "MAX_UNSUPPORTED_CLAIMS",
    "ORIGINS_TARGET",
    "RANK_WEIGHTS",
    "RankedCandidate",
    "RejectedCandidate",
    "RoleRunner",
    "ROLES",
    "ROLE_SPECS",
    "SCENARIO_LABELS",
    "SemanticCitationAuditor",
    "TournamentResult",
    "audit_citations",
    "build_role_prompt",
    "role_output_schema",
    "run_tournament",
    "select_prompt_evidence",
    "resolve_candidate_entities",
    "resolve_evidence_market_identity",
    "validate_audit_decision",
]
