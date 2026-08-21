import itertools
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_intelligence.contracts import (
    EvidenceType,
    NormalizedEntity,
    NormalizedEvidence,
    Scenario,
)
from thesis_fusion import canonical_thesis_key
from thesis_scoring import assess_opportunity, scenario_valuation
from thesis_tournament import (
    CITATION_FIELDS,
    MAX_PER_ROLE,
    MAX_PROMPT_EVIDENCE,
    MAX_RAW_CANDIDATES,
    ROLES,
    CandidateDraft,
    CitationFinding,
    CitationVerdict,
    audit_citations,
    build_role_prompt,
    resolve_candidate_entities,
    role_output_schema,
    run_tournament,
    select_prompt_evidence,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
THEME_ID = "a1b2c3d4-e5f6-4789-8abc-def012345678"


class FakeRunner:
    """Injectable RoleRunner: returns canned outputs per role."""

    def __init__(self, outputs=None, *, fail_roles=()):
        self.outputs = dict(outputs or {})
        self.fail_roles = set(fail_roles)
        self.calls = []

    def run(self, *, role, prompt, schema):
        self.calls.append({"role": role, "prompt": prompt, "schema": schema})
        if role in self.fail_roles:
            raise RuntimeError("model unavailable")
        return self.outputs.get(role, [])


class FakeAuditor:
    """Injectable SemanticCitationAuditor with canned decisions."""

    def __init__(self, decisions=None, *, fail=False):
        self.decisions = list(decisions or [])
        self.fail = fail
        self.calls = []

    def audit(self, *, candidates, evidence):
        self.calls.append({"candidates": candidates, "evidence": evidence})
        if self.fail:
            raise RuntimeError("auditor unavailable")
        return self.decisions


def evidence(**overrides):
    base = {
        "evidence_type": EvidenceType.SOURCE_CLAIM,
        "evidence_id": "ev-1",
        "source_name": "reuters",
        "source_timestamp": NOW,
        "title": "demand accelerates",
        "bounded_excerpt": "orders rose 15 percent in the quarter",
        "entities": [
            NormalizedEntity.create(
                entity_type="company", normalized_key="nvidia", display_name="Nvidia"
            ),
            NormalizedEntity.create(
                entity_type="symbol", normalized_key="nvda", display_name="NVDA"
            ),
        ],
    }
    base.update(overrides)
    return NormalizedEvidence.create(**base)


def candidate(**overrides):
    base = {
        "claim": "revenue growth reaccelerates",
        "subject": "nvidia",
        "instrument": "NVDA",
        "direction": "long",
        "horizon": "months",
        "consensus": "market expects growth to decelerate",
        "variant_perception": "growth reaccelerates instead",
        "mechanism": "capacity constraints ease",
        "catalyst": "next earnings release",
        "trend_context": "the cited evidence reports an improving operating trend",
        "valuation_context": "the cited evidence provides current valuation context",
        "sentiment_context": "the cited evidence provides dated expectations context",
        "scenarios": {
            "bull": {
                "probability": 0.3,
                "expected_return": 0.4,
                "description": "capacity frees up and demand holds",
            },
            "base": {
                "probability": 0.5,
                "expected_return": 0.1,
                "description": "capacity stays tight but demand stays firm",
            },
            "bear": {
                "probability": 0.2,
                "expected_return": -0.3,
                "description": "capacity stays constrained and demand softens",
            },
        },
        "invalidators": ["capacity remains constrained"],
        "missing_evidence": ["unit shipment data"],
        "evidence_refs": ["source_claim:ev-1"],
        "confidence": 0.6,
    }
    base.update(overrides)
    if "citations" not in overrides:
        refs = list(base["evidence_refs"])
        base["citations"] = {field: refs for field in CITATION_FIELDS}
    return base


def run(evidence_list, runner, **kwargs):
    options = {"theme_id": THEME_ID, "as_of": NOW}
    options.update(kwargs)
    return run_tournament(runner=runner, evidence=evidence_list, **options)


class CandidateEntityResolutionTests(unittest.TestCase):
    def test_resolves_company_and_symbol_only_from_cited_evidence(self):
        item = evidence(
            entities=[
                NormalizedEntity.create(
                    "company",
                    "intermediate-capital-group-icg",
                    "Intermediate Capital Group (ICG)",
                ),
                NormalizedEntity.create("symbol", "icg-l", "ICG.L"),
            ]
        )
        raw = candidate(
            subject="Intermediate Capital Group",
            instrument="Intermediate Capital Group ordinary shares",
        )
        result = run(
            [item],
            FakeRunner({"fundamental": [raw]}),
            roles=("fundamental",),
        )

        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(
            resolve_candidate_entities(result.ranked[0].candidate, [item]),
            ("Intermediate Capital Group (ICG)", "ICG.L"),
        )

    def test_ambiguous_evidence_symbols_remain_unknown(self):
        item = evidence(
            entities=[
                NormalizedEntity.create("company", "acme", "Acme"),
                NormalizedEntity.create("symbol", "acme-a", "ACME.A"),
                NormalizedEntity.create("symbol", "acme-b", "ACME.B"),
            ]
        )
        raw = candidate(subject="Acme", instrument="Acme ordinary shares")
        result = run(
            [item],
            FakeRunner({"fundamental": [raw]}),
            roles=("fundamental",),
        )

        self.assertEqual(
            resolve_candidate_entities(result.ranked[0].candidate, [item]),
            ("Acme", None),
        )


class GenerationAndCompactionTests(unittest.TestCase):
    def test_duplicate_role_outputs_compact_without_added_evidence(self):
        item = evidence()
        duplicate = candidate()
        runner = FakeRunner({"fundamental": [duplicate], "macro_regime": [duplicate]})
        result = run(
            [item],
            runner,
            roles=("fundamental", "macro_regime"),
        )
        self.assertEqual(result.raw_candidate_count, 2)
        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(len(result.compacted), 1)
        self.assertEqual(
            result.compacted[0].candidate_key,
            result.ranked[0].candidate.candidate_key,
        )
        self.assertIn(
            "adds no evidence",
            result.compacted[0].note,
        )
        # The compacted candidate carries the same single citation.
        self.assertEqual(
            result.ranked[0].candidate.evidence_refs,
            ("source_claim:ev-1",),
        )

        baseline = run(
            [item],
            FakeRunner({"fundamental": [duplicate]}),
            roles=("fundamental",),
        )
        self.assertEqual(len(baseline.ranked), 1)
        self.assertEqual(len(baseline.compacted), 0)
        merged = result.ranked[0]
        alone = baseline.ranked[0]
        self.assertEqual(merged.candidate.evidence_refs, alone.candidate.evidence_refs)
        self.assertEqual(merged.coverage, alone.coverage)
        self.assertEqual(merged.rank_score, alone.rank_score)
        self.assertEqual(merged.evidence.support_mass, alone.evidence.support_mass)
        self.assertEqual(
            merged.evidence.unique_evidence_count,
            alone.evidence.unique_evidence_count,
        )

    def test_repeated_agreement_never_adds_evidence_mass(self):
        item = evidence()
        shared = candidate()
        runner = FakeRunner(
            {
                "evidence_extractor": [shared],
                "fundamental": [shared],
                "expectations_revisions": [shared],
            }
        )
        result = run([item], runner, roles=ROLES[:3])
        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(len(result.compacted), 2)
        self.assertEqual(result.ranked[0].evidence.support_count, 1)
        self.assertEqual(result.ranked[0].evidence.evidence_input_count, 1)

    def test_opposing_directions_coexist(self):
        item = evidence()
        bull = candidate()
        bear = candidate(direction="short")
        runner = FakeRunner({"fundamental": [bull], "contrarian": [bear]})
        result = run(
            [item],
            runner,
            roles=("fundamental", "contrarian"),
        )
        self.assertEqual(len(result.ranked), 2)
        self.assertEqual(len(result.compacted), 0)
        directions = {entry.candidate.direction for entry in result.ranked}
        self.assertEqual(directions, {"long", "short"})
        keys = {entry.candidate.candidate_key for entry in result.ranked}
        self.assertEqual(len(keys), 2)

    def test_all_roles_participate_with_distinct_candidates(self):
        item = evidence()
        runner = FakeRunner(
            {role: [candidate(mechanism=f"mechanism {role}")] for role in ROLES}
        )
        result = run([item], runner)
        self.assertEqual(result.raw_candidate_count, len(ROLES))
        self.assertEqual(len(result.ranked), len(ROLES))
        roles_seen = {entry.candidate.role for entry in result.ranked}
        self.assertEqual(roles_seen, set(ROLES))

    def test_identical_claims_across_roles_stay_one_candidate(self):
        item = evidence()
        first = candidate()
        second = candidate(consensus="slightly different consensus wording")
        runner = FakeRunner({"fundamental": [first], "editor": [second]})
        result = run(
            [item],
            runner,
            roles=("fundamental", "editor"),
        )
        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(len(result.compacted), 1)


class ScenarioDescriptionTests(unittest.TestCase):
    """Bounded nonblank path/assumptions descriptions per scenario leg."""

    def test_paths_round_trip_through_parse_and_rank(self):
        item = evidence()
        raw = candidate()
        result = run([item], FakeRunner({"fundamental": [raw]}), roles=("fundamental",))
        self.assertEqual(len(result.ranked), 1)
        draft = result.ranked[0].candidate
        self.assertEqual(
            draft.scenario_paths,
            (
                raw["scenarios"]["bull"]["description"],
                raw["scenarios"]["base"]["description"],
                raw["scenarios"]["bear"]["description"],
            ),
        )
        payload = draft.to_dict()
        self.assertEqual(payload["scenario_paths"], list(draft.scenario_paths))

    def test_paths_survive_compaction(self):
        item = evidence()
        duplicate = candidate()
        runner = FakeRunner({"fundamental": [duplicate], "macro_regime": [duplicate]})
        result = run(
            [item],
            runner,
            roles=("fundamental", "macro_regime"),
        )
        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(len(result.compacted), 1)
        draft = result.ranked[0].candidate
        self.assertEqual(
            draft.scenario_paths,
            (
                duplicate["scenarios"]["bull"]["description"],
                duplicate["scenarios"]["base"]["description"],
                duplicate["scenarios"]["bear"]["description"],
            ),
        )

    def test_paths_participate_in_the_content_fingerprint(self):
        item = evidence()
        original = candidate()
        reworded = candidate(
            scenarios={
                "bull": {
                    "probability": 0.3,
                    "expected_return": 0.4,
                    "description": "demand holds while capacity frees up",
                },
                "base": original["scenarios"]["base"],
                "bear": original["scenarios"]["bear"],
            }
        )
        baseline = run(
            [item],
            FakeRunner({"fundamental": [original]}),
            roles=("fundamental",),
        )
        changed = run(
            [item],
            FakeRunner({"fundamental": [reworded]}),
            roles=("fundamental",),
        )
        self.assertEqual(len(baseline.ranked), 1)
        self.assertEqual(len(changed.ranked), 1)
        self.assertNotEqual(
            baseline.ranked[0].candidate.content_fingerprint,
            changed.ranked[0].candidate.content_fingerprint,
        )
        # The canonical thesis key ignores scenario content: only the
        # description changed, so the two candidates share one identity.
        self.assertEqual(
            baseline.ranked[0].candidate.candidate_key,
            changed.ranked[0].candidate.candidate_key,
        )


class RejectionTests(unittest.TestCase):
    def test_unknown_citations_reject(self):
        item = evidence()
        runner = FakeRunner(
            {"fundamental": [candidate(evidence_refs=["source_claim:ghost"])]}
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("invalid evidence citation", result.rejected[0].reason)
        self.assertIn("ghost", result.rejected[0].reason)

    def test_evidence_refs_must_equal_field_citation_union(self):
        first = evidence()
        second = evidence(evidence_id="ev-2", source_name="sec")
        raw = candidate(
            evidence_refs=[first.ref, second.ref],
            citations={field: [first.ref] for field in CITATION_FIELDS},
        )

        result = run(
            [first, second],
            FakeRunner({"fundamental": [raw]}),
            roles=("fundamental",),
        )

        self.assertEqual(result.ranked, ())
        self.assertIn(
            "evidence_refs must equal the deduplicated union",
            result.rejected[0].reason,
        )

    def test_numeric_field_requires_support_from_its_own_citations(self):
        numeric = evidence(bounded_excerpt="orders rose 77 percent in the quarter")
        nonnumeric = evidence(
            evidence_id="ev-2",
            source_name="sec",
            bounded_excerpt="orders increased during the quarter",
        )
        raw = candidate(
            claim="orders rose 77 percent",
            evidence_refs=[numeric.ref, nonnumeric.ref],
        )
        raw["citations"]["claim"] = [nonnumeric.ref]

        result = run(
            [numeric, nonnumeric],
            FakeRunner({"fundamental": [raw]}),
            roles=("fundamental",),
        )

        self.assertEqual(result.ranked, ())
        self.assertIn(
            "unsupported numeric claim in claim",
            result.rejected[0].reason,
        )

    def test_embedded_evidence_reference_in_prose_rejects(self):
        item = evidence()
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(claim="orders rose per source_claim:ev-1 source")
                ]
            }
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("dedicated fields", result.rejected[0].reason)

    def test_extra_keys_and_content_injection_reject(self):
        item = evidence()
        smuggled = candidate(evidence_content={"claim": "fabricated"})
        runner = FakeRunner({"fundamental": [smuggled]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("unexpected keys", result.rejected[0].reason)
        self.assertIn("evidence_content", result.rejected[0].reason)

    def test_evidence_object_instead_of_refs_rejects(self):
        item = evidence()
        runner = FakeRunner(
            {"fundamental": [candidate(evidence_refs=[item.to_dict()])]}
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("must be a string", result.rejected[0].reason)

    def test_runner_cannot_inject_unsupported_evidence_through_any_field(self):
        item = evidence()
        # A fabricated ref in the catalyst field is an embedded citation.
        embedded = candidate(catalyst="reprice after source_claim:ghost breaks")
        runner = FakeRunner({"fundamental": [embedded]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertNotIn(
            "source_claim:ghost",
            [entry.candidate.candidate_key for entry in result.ranked],
        )

    def test_prohibited_trade_language_rejects(self):
        item = evidence()
        runner = FakeRunner(
            {"fundamental": [candidate(claim="investors should buy the stock")]}
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("prohibited trade language", result.rejected[0].reason)

    def test_unsupported_numeric_claim_rejects(self):
        item = evidence()
        runner = FakeRunner({"fundamental": [candidate(claim="orders rose 8 percent")]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("unsupported numeric claim", result.rejected[0].reason)

    def test_supported_numeric_claim_promotes(self):
        item = evidence()
        runner = FakeRunner(
            {"fundamental": [candidate(claim="orders rose 15 percent")]}
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 1)

    def test_incomplete_competitors_do_not_promote(self):
        item = evidence()
        cases = [
            candidate(claim=""),
            candidate(mechanism=""),
            candidate(consensus=""),
            candidate(
                scenarios={
                    "bull": {"probability": 0.3, "expected_return": 0.4},
                    "base": {"probability": 0.5, "expected_return": 0.1},
                }
            ),
            candidate(evidence_refs=[]),
            candidate(subject=""),
            candidate(invalidators=[]),
            candidate(
                scenarios={
                    "bull": {
                        "probability": 0.3,
                        "expected_return": 0.4,
                        "description": "",
                    },
                    "base": {
                        "probability": 0.5,
                        "expected_return": 0.1,
                        "description": "base path",
                    },
                    "bear": {
                        "probability": 0.2,
                        "expected_return": -0.3,
                        "description": "bear path",
                    },
                }
            ),
        ]
        for raw in cases:
            runner = FakeRunner({"fundamental": [raw]})
            result = run([item], runner, roles=("fundamental",))
            self.assertEqual(
                len(result.ranked),
                0,
                f"candidate should not promote: {raw}",
            )
            self.assertEqual(len(result.rejected), 1)
            reason = result.rejected[0].reason
            self.assertTrue(
                "incomplete candidate does not promote" in reason
                or "must contain exactly bull, base, and bear" in reason
                or "at least one supplied evidence reference" in reason
                or "citation" in reason
                or "description is required" in reason,
                reason,
            )

    def test_blank_scenario_description_rejects_promotion(self):
        item = evidence()
        scenarios = candidate()["scenarios"]
        blank = dict(scenarios)
        blank["bear"] = {
            "probability": 0.2,
            "expected_return": -0.3,
            "description": "   ",
        }
        result = run(
            [item],
            FakeRunner({"fundamental": [candidate(scenarios=blank)]}),
            roles=("fundamental",),
        )
        self.assertEqual(len(result.ranked), 0)
        self.assertIn(
            "scenario bear description is required", result.rejected[0].reason
        )

    def test_overlong_scenario_description_rejects(self):
        item = evidence()
        scenarios = candidate()["scenarios"]
        long_leg = dict(scenarios)
        long_leg["bear"] = {
            "probability": 0.2,
            "expected_return": -0.3,
            "description": "x" * (2000 + 1),
        }
        result = run(
            [item],
            FakeRunner({"fundamental": [candidate(scenarios=long_leg)]}),
            roles=("fundamental",),
        )
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("exceeds maximum length", result.rejected[0].reason)

    def test_empty_invalidators_reject_promotion(self):
        item = evidence()
        result = run(
            [item],
            FakeRunner(
                {"fundamental": [candidate(invalidators=[], missing_evidence=[])]}
            ),
            roles=("fundamental",),
        )
        self.assertEqual(len(result.ranked), 0)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("invalidators", result.rejected[0].reason)

    def test_invalid_direction_and_horizon_reject(self):
        item = evidence()
        for raw in (
            candidate(direction="buy"),
            candidate(horizon="forever"),
        ):
            runner = FakeRunner({"fundamental": [raw]})
            result = run([item], runner, roles=("fundamental",))
            self.assertEqual(len(result.ranked), 0)
            self.assertIn("invalid", result.rejected[0].reason)

    def test_invalid_scenario_leg_rejects(self):
        item = evidence()
        scenarios = candidate()["scenarios"]
        bad = dict(scenarios)
        bad["bear"] = {
            "description": "bear path",
            "probability": 1.5,
            "expected_return": -0.3,
        }
        runner = FakeRunner({"fundamental": [candidate(scenarios=bad)]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("scenario bear is invalid", result.rejected[0].reason)

    def test_null_scenario_probability_is_allowed(self):
        item = evidence()
        scenarios = candidate()["scenarios"]
        unknown = dict(scenarios)
        unknown["bear"] = {
            "description": "bear path",
            "probability": None,
            "expected_return": -0.3,
        }
        runner = FakeRunner(
            {"fundamental": [candidate(scenarios=unknown, confidence=None)]}
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 1)
        self.assertIsNone(result.ranked[0].candidate.scenarios[2].probability)

    def test_confidence_out_of_range_rejects(self):
        item = evidence()
        runner = FakeRunner({"fundamental": [candidate(confidence=1.5)]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("confidence", result.rejected[0].reason)

    def test_non_point_in_time_safe_evidence_rejects(self):
        item = evidence(point_in_time_safe=False)
        runner = FakeRunner({"fundamental": [candidate()]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("point-in-time", result.rejected[0].reason)

    def test_role_runner_failure_is_soft(self):
        item = evidence()
        runner = FakeRunner(
            {"contrarian": [candidate(mechanism="mechanism contrarian")]},
            fail_roles=("fundamental",),
        )
        result = run(
            [item],
            runner,
            roles=("fundamental", "contrarian"),
        )
        self.assertEqual(len(result.ranked), 1)
        failed = [entry for entry in result.rejected if entry.role == "fundamental"]
        self.assertEqual(len(failed), 1)
        self.assertIn("role runner failed", failed[0].reason)

    def test_non_list_role_output_rejects(self):
        item = evidence()
        runner = FakeRunner({"fundamental": {"candidates": []}})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("must be a JSON array", result.rejected[0].reason)

    def test_empty_role_output_is_fine(self):
        item = evidence()
        runner = FakeRunner({})
        result = run([item], runner)
        self.assertEqual(len(result.ranked), 0)
        self.assertEqual(len(result.rejected), 0)


class BoundsAndRankingTests(unittest.TestCase):
    def test_raw_and_per_role_bounds_hold(self):
        items = [evidence(evidence_id=f"ev-{i}") for i in range(3)]
        mechanisms = [
            "mechanism " + a + b
            for a, b in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=2)
        ]
        runner = FakeRunner(
            {
                role: [
                    candidate(
                        mechanism=mechanisms[offset * 40 + i],
                        evidence_refs=["source_claim:ev-0"],
                    )
                    for i in range(40)
                ]
                for offset, role in enumerate(ROLES)
            }
        )
        result = run(items, runner)
        self.assertEqual(result.raw_candidate_count, MAX_RAW_CANDIDATES)
        per_role = [
            entry
            for entry in result.rejected
            if entry.reason == "per-role candidate bound exceeded"
        ]
        self.assertEqual(len(per_role), len(ROLES) * (40 - MAX_PER_ROLE))
        self.assertLessEqual(result.raw_candidate_count, MAX_RAW_CANDIDATES)
        self.assertEqual(result.bounds["max_raw_candidates"], MAX_RAW_CANDIDATES)
        self.assertEqual(result.bounds["max_per_role"], MAX_PER_ROLE)

    def test_raw_cap_binds(self):
        items = [evidence(evidence_id=f"ev-{i}") for i in range(3)]
        mechanisms = [
            "mechanism " + a + b
            for a, b in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=2)
        ]
        runner = FakeRunner(
            {
                role: [
                    candidate(
                        mechanism=mechanisms[offset * MAX_PER_ROLE + i],
                        evidence_refs=["source_claim:ev-0"],
                    )
                    for i in range(MAX_PER_ROLE)
                ]
                for offset, role in enumerate(ROLES)
            }
        )
        result = run(items, runner, max_raw_candidates=100)
        self.assertEqual(result.raw_candidate_count, 100)
        capped = [
            entry
            for entry in result.rejected
            if entry.reason == "raw candidate bound exceeded"
        ]
        self.assertEqual(len(capped), MAX_RAW_CANDIDATES - 100)

    def test_promotion_bound_holds(self):
        items = [evidence(evidence_id=f"ev-{i}") for i in range(3)]
        mechanisms = [
            "mechanism " + a + b
            for a, b in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=2)
        ]
        runner = FakeRunner(
            {
                role: [
                    candidate(
                        mechanism=mechanisms[offset * MAX_PER_ROLE + i],
                        evidence_refs=["source_claim:ev-0"],
                    )
                    for i in range(MAX_PER_ROLE)
                ]
                for offset, role in enumerate(ROLES)
            }
        )
        result = run(items, runner, max_promoted=64)
        self.assertEqual(result.raw_candidate_count, MAX_RAW_CANDIDATES)
        self.assertEqual(len(result.ranked), 64)
        self.assertEqual([entry.rank for entry in result.ranked], list(range(1, 65)))
        bounded = [
            entry
            for entry in result.rejected
            if entry.reason == "promotion bound exceeded"
        ]
        self.assertEqual(len(bounded), MAX_RAW_CANDIDATES - 64)

    def test_deterministic_ordering_survives_input_permutations(self):
        items = [
            evidence(evidence_id=f"ev-{i}", source_name=source)
            for i, source in enumerate(("reuters", "fred", "cboe"))
        ]
        refs = [f"source_claim:ev-{i}" for i in range(3)]
        candidates_by_role = {
            "fundamental": [
                candidate(
                    mechanism="mechanism alpha",
                    evidence_refs=[refs[0]],
                    confidence=0.9,
                ),
                candidate(
                    mechanism="mechanism beta",
                    evidence_refs=refs,
                    confidence=0.2,
                    invalidators=[],
                    missing_evidence=[],
                ),
                candidate(
                    mechanism="mechanism gamma",
                    evidence_refs=refs[:2],
                    confidence=0.5,
                ),
                candidate(
                    mechanism="mechanism delta",
                    evidence_refs=refs,
                    confidence=0.7,
                ),
            ],
            "contrarian": [
                candidate(
                    mechanism="mechanism epsilon",
                    evidence_refs=[refs[0], refs[1]],
                    confidence=0.4,
                ),
            ],
        }
        forward = run(
            items,
            FakeRunner(candidates_by_role),
            roles=("fundamental", "contrarian"),
        )
        reversed_map = {
            role: list(reversed(outputs))
            for role, outputs in candidates_by_role.items()
        }
        backward = run(
            items,
            FakeRunner(reversed_map),
            roles=("contrarian", "fundamental"),
        )
        self.assertEqual(len(forward.ranked), len(backward.ranked))
        self.assertEqual(
            [entry.candidate.candidate_key for entry in forward.ranked],
            [entry.candidate.candidate_key for entry in backward.ranked],
        )
        self.assertEqual(
            [entry.rank_score for entry in forward.ranked],
            [entry.rank_score for entry in backward.ranked],
        )
        self.assertEqual(
            [entry.rank for entry in forward.ranked],
            [entry.rank for entry in backward.ranked],
        )

    def test_ranking_uses_evidence_not_model_confidence_alone(self):
        items = [
            evidence(evidence_id=f"ev-{i}", source_name=source)
            for i, source in enumerate(("reuters", "fred", "cboe"))
        ]
        refs = [f"source_claim:ev-{i}" for i in range(3)]
        well_supported = candidate(
            mechanism="mechanism solid",
            evidence_refs=refs,
            confidence=0.2,
        )
        thinly_supported = candidate(
            mechanism="mechanism thin",
            evidence_refs=[refs[0]],
            confidence=0.9,
        )
        runner = FakeRunner({"fundamental": [well_supported, thinly_supported]})
        result = run(items, runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 2)
        self.assertEqual(result.ranked[0].candidate.mechanism, "mechanism solid")
        self.assertGreater(result.ranked[0].coverage, result.ranked[1].coverage)
        self.assertLess(
            result.ranked[0].candidate.confidence,
            result.ranked[1].candidate.confidence,
        )

    def test_completeness_influences_rank(self):
        items = [
            evidence(evidence_id=f"ev-{i}", source_name=source)
            for i, source in enumerate(("reuters", "fred", "cboe"))
        ]
        refs = [f"source_claim:ev-{i}" for i in range(3)]
        complete = candidate(
            mechanism="mechanism explicit",
            evidence_refs=refs,
            invalidators=["capacity remains constrained"],
            missing_evidence=["unit shipment data"],
        )
        sparse = candidate(
            mechanism="mechanism sparse",
            evidence_refs=refs,
            invalidators=["capacity remains constrained"],
            missing_evidence=[],
        )
        runner = FakeRunner({"fundamental": [complete, sparse]})
        result = run(items, runner, roles=("fundamental",))
        self.assertEqual(result.ranked[0].candidate.mechanism, "mechanism explicit")
        self.assertEqual(result.ranked[0].candidate.completeness, 1.0)
        self.assertLess(result.ranked[1].candidate.completeness, 1.0)


class ScoringIntegrationTests(unittest.TestCase):
    def test_scenario_valuation_uses_thesis_scoring(self):
        item = evidence()
        raw = candidate()
        runner = FakeRunner({"fundamental": [raw]})
        result = run([item], runner, roles=("fundamental",), cost=0.05)
        self.assertEqual(len(result.ranked), 1)
        legs = [
            Scenario.create(
                label=label,
                probability=raw["scenarios"][label]["probability"],
                expected_return=raw["scenarios"][label]["expected_return"],
            )
            for label in ("bull", "base", "bear")
        ]
        expected = scenario_valuation(legs, cost=0.05)
        valuation = result.ranked[0].valuation
        self.assertAlmostEqual(valuation.expected_value, expected.expected_value)
        self.assertAlmostEqual(
            valuation.expected_shortfall, expected.expected_shortfall
        )
        self.assertEqual(
            valuation.missing_probability_labels,
            expected.missing_probability_labels,
        )
        # 0.3*0.4 + 0.5*0.1 + 0.2*(-0.3) - 0.05
        self.assertAlmostEqual(valuation.expected_value, 0.06)

    def test_opportunity_gates_use_assess_opportunity(self):
        items = [
            evidence(evidence_id=f"ev-{i}", source_name=source)
            for i, source in enumerate(("reuters", "fred", "cboe"))
        ]
        refs = [f"source_claim:ev-{i}" for i in range(3)]
        runner = FakeRunner({"fundamental": [candidate(evidence_refs=refs)]})
        result = run(
            items,
            runner,
            roles=("fundamental",),
            attention=0.1,
            crowding=0.1,
            liquidity=0.7,
            downside=0.2,
        )
        self.assertEqual(len(result.ranked), 1)
        ranked = result.ranked[0]
        expected = assess_opportunity(
            evidence_strength=ranked.evidence.support_mass,
            confidence=ranked.evidence.confidence,
            neglect=0.9,
            catalyst_ready=0.5,
            liquidity=0.7,
            downside=0.2,
        )
        self.assertAlmostEqual(ranked.opportunity.opportunity, expected.opportunity)
        self.assertGreater(ranked.opportunity.opportunity, 0.0)
        self.assertEqual(ranked.opportunity.blocked_by, ())

    def test_missing_opportunity_components_block_explicitly(self):
        items = [
            evidence(evidence_id=f"ev-{i}", source_name=source)
            for i, source in enumerate(("reuters", "fred", "cboe"))
        ]
        refs = [f"source_claim:ev-{i}" for i in range(3)]
        runner = FakeRunner({"fundamental": [candidate(evidence_refs=refs)]})
        result = run(items, runner, roles=("fundamental",))
        ranked = result.ranked[0]
        self.assertEqual(ranked.opportunity.opportunity, 0.0)
        self.assertIn("liquidity", ranked.opportunity.blocked_by)
        self.assertIn("downside", ranked.opportunity.blocked_by)
        self.assertIn("neglect", ranked.opportunity.blocked_by)

    def test_support_mass_reflects_cited_evidence_only(self):
        items = [
            evidence(evidence_id=f"ev-{i}", source_name=source)
            for i, source in enumerate(("reuters", "fred"))
        ]
        runner = FakeRunner(
            {"fundamental": [candidate(evidence_refs=["source_claim:ev-0"])]}
        )
        result = run(items, runner, roles=("fundamental",))
        ranked = result.ranked[0]
        self.assertEqual(ranked.evidence.evidence_input_count, 1)
        self.assertEqual(ranked.evidence.unique_evidence_count, 1)
        self.assertEqual(ranked.independent_origins, 1)
        self.assertEqual(ranked.coverage_refs, ("source_claim:ev-0",))


class PromptAndSchemaTests(unittest.TestCase):
    def test_role_prompt_contains_contract_fields(self):
        item = evidence(evidence_id="ev-7")
        prompt = build_role_prompt(
            role="fundamental",
            theme_id=THEME_ID,
            subject="nvidia",
            evidence=[item],
            as_of=NOW,
        )
        for token in (
            "Fundamental Analyst",
            "source_claim:ev-7",
            "evidence_refs",
            "variant_perception",
            "bull",
            "base",
            "bear",
            "probability",
            "invalidators",
            "missing_evidence",
            "long, short, neutral",
            THEME_ID,
            "point-in-time",
            "Never round, rescale, convert units",
        ):
            self.assertIn(token, prompt)

    def test_every_role_has_a_production_prompt(self):
        item = evidence()
        for role in ROLES:
            prompt = build_role_prompt(
                role=role,
                theme_id=THEME_ID,
                subject=None,
                evidence=[item],
            )
            self.assertIn("MISSION", prompt)
            self.assertIn("HARD RULES", prompt)
            self.assertIn("SUPPLIED EVIDENCE", prompt)

    def test_output_schema_is_strict(self):
        schema = role_output_schema()
        self.assertEqual(schema["type"], "array")
        items = schema["items"]
        self.assertFalse(items["additionalProperties"])
        for field in (
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
        ):
            self.assertIn(field, items["required"])
        self.assertEqual(
            items["properties"]["direction"]["enum"], ["long", "neutral", "short"]
        )
        self.assertEqual(
            items["properties"]["scenarios"]["required"],
            ["bull", "base", "bear"],
        )
        self.assertEqual(items["properties"]["evidence_refs"]["minItems"], 1)
        self.assertEqual(
            items["properties"]["citations"]["required"],
            list(CITATION_FIELDS),
        )

    def test_prompt_scenario_contract_matches_schema(self):
        item = evidence()
        prompt = build_role_prompt(
            role="editor",
            theme_id=THEME_ID,
            subject=None,
            evidence=[item],
        )
        schema = role_output_schema()
        leg_schema = schema["items"]["properties"]["scenarios"]["properties"]
        # Probability is nullable in both the prompt and the schema.
        self.assertIn("null", leg_schema["bull"]["properties"]["probability"]["type"])
        self.assertIn(
            "probability",
            prompt,
        )
        # Expected return is a required, bounded number in both: the schema
        # type is number-only and the prompt must never permit null.
        self.assertEqual(
            leg_schema["bull"]["properties"]["expected_return"]["type"],
            "number",
        )
        self.assertIn("required — never null", prompt)
        self.assertIn("Expected returns are explicit", prompt)
        # Every leg carries a bounded nonblank path/assumptions description
        # in both the prompt and the schema.
        self.assertEqual(
            leg_schema["bull"]["required"],
            ["description", "probability", "expected_return"],
        )
        self.assertEqual(
            leg_schema["bull"]["properties"]["description"]["type"],
            "string",
        )
        self.assertEqual(
            leg_schema["bull"]["properties"]["description"]["minLength"],
            1,
        )
        self.assertIn("path", prompt)
        self.assertIn("never blank", prompt)
        self.assertIn("at least one is required for promotion", prompt)
        # Fractional units are unambiguous: fractions, never percentage points.
        self.assertIn("0.20 means +20%", prompt)
        self.assertIn("-0.15 means -15%", prompt)
        self.assertIn("never emit percentage points", prompt)
        self.assertNotIn(
            "expected returns in scenarios are your judgment and may",
            prompt,
        )
        self.assertNotIn("may be null for expected_return", prompt)

    def test_prompt_and_schema_are_passed_to_runner(self):
        item = evidence()
        runner = FakeRunner({})
        run([item], runner, roles=("fundamental",))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["role"], "fundamental")
        self.assertIn("SUPPLIED EVIDENCE", runner.calls[0]["prompt"])
        self.assertFalse(runner.calls[0]["schema"]["items"]["additionalProperties"])

    def test_prompt_evidence_selection_round_robins_across_symbols(self):
        def symbol_item(evidence_id, symbol, at):
            return evidence(
                evidence_id=evidence_id,
                source_name="reuters",
                source_timestamp=at,
                entities=[
                    NormalizedEntity.create(
                        entity_type="symbol",
                        normalized_key=symbol,
                        display_name=symbol.upper(),
                    )
                ],
            )

        rows = []
        for index in range(240):
            rows.append(
                symbol_item(f"nvda{index:03d}", "nvda", NOW - timedelta(minutes=index))
            )
        for index in range(5):
            rows.append(
                symbol_item(
                    f"aapl{index:03d}", "aapl", NOW - timedelta(minutes=300 + index)
                )
            )
        for index in range(5):
            rows.append(
                symbol_item(
                    f"msft{index:03d}", "msft", NOW - timedelta(minutes=600 + index)
                )
            )
        self.assertEqual(len(rows), 250)

        selected = select_prompt_evidence(rows)
        self.assertEqual(len(selected), MAX_PROMPT_EVIDENCE)
        selected_refs = {item.ref for item in selected}
        # Same source and evidence type, but the rare symbols must survive
        # the dominant-symbol skew via the entity-dimension group key.
        self.assertTrue(
            any(ref.startswith("source_claim:aapl") for ref in selected_refs)
        )
        self.assertTrue(
            any(ref.startswith("source_claim:msft") for ref in selected_refs)
        )
        self.assertEqual(len(rows) - len(selected), 50)

        # Permutation invariant: reversed input selects the identical set.
        again = select_prompt_evidence(list(reversed(rows)))
        self.assertEqual(
            [item.ref for item in selected],
            [item.ref for item in again],
        )
        prompt = build_role_prompt(
            role="fundamental",
            theme_id=THEME_ID,
            subject=None,
            evidence=rows,
        )
        self.assertIn("(50 further supplied items omitted)", prompt)


class EntityGroundingTests(unittest.TestCase):
    def test_arbitrary_entity_from_unrelated_evidence_rejects(self):
        apple = evidence(
            evidence_id="ev-apple",
            title="apple supply",
            entities=[
                NormalizedEntity.create(
                    entity_type="company", normalized_key="apple", display_name="Apple"
                ),
                NormalizedEntity.create(
                    entity_type="symbol", normalized_key="aapl", display_name="AAPL"
                ),
            ],
        )
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(
                        subject="tesla",
                        instrument="TSLA",
                        evidence_refs=["source_claim:ev-apple"],
                    )
                ]
            }
        )
        result = run([apple], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("ungrounded candidate entity", result.rejected[0].reason)

    def test_exact_company_and_symbol_grounding_passes(self):
        item = evidence(
            evidence_id="ev-exmp",
            title="example corp demand",
            entities=[
                NormalizedEntity.create(
                    entity_type="company",
                    normalized_key="example-corp",
                    display_name="Example Corp",
                ),
                NormalizedEntity.create(
                    entity_type="symbol", normalized_key="exmp", display_name="EXMP"
                ),
            ],
        )
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(
                        subject="Example Corp",
                        instrument="EXMP",
                        evidence_refs=["source_claim:ev-exmp"],
                    )
                ]
            }
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 1)

    def test_macro_region_and_concept_grounding_passes(self):
        item = evidence(
            evidence_id="ev-macro",
            title="us inflation",
            entities=[
                NormalizedEntity.create(
                    entity_type="macro_region", normalized_key="us", display_name="US"
                ),
                NormalizedEntity.create(
                    entity_type="concept", normalized_key="cpi", display_name="CPI"
                ),
                NormalizedEntity.create(
                    entity_type="concept",
                    normalized_key="inflation",
                    display_name="Inflation",
                ),
            ],
        )
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(
                        subject="US",
                        instrument="CPI",
                        evidence_refs=["source_claim:ev-macro"],
                    )
                ]
            }
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 1)

    def test_evidence_without_entities_requires_subject_constraint(self):
        item = evidence(entities=[])
        runner = FakeRunner({"fundamental": [candidate()]})
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 0)
        self.assertIn("no grounding entities", result.rejected[0].reason)
        constrained = run(
            [item],
            FakeRunner({"fundamental": [candidate()]}),
            roles=("fundamental",),
            subject="nvidia",
        )
        self.assertEqual(len(constrained.ranked), 1)


class SemanticAuditTests(unittest.TestCase):
    def _keys(self, *mechanisms):
        return {
            mechanism: canonical_thesis_key(
                theme_id=THEME_ID,
                subject="nvidia",
                direction="long",
                horizon="months",
                mechanism=mechanism,
            )
            for mechanism in mechanisms
        }

    def _decision(
        self,
        key,
        verdict="entailed",
        refs=None,
        unsupported=(),
        rationale="checked against cited excerpts",
    ):
        return {
            "candidate_key": key,
            "verdict": verdict,
            "cited_refs": list(refs) if refs is not None else [],
            "unsupported_claims": list(unsupported),
            "rationale": rationale,
        }

    def test_optional_auditor_gates_promotion_by_verdict(self):
        item = evidence()
        keys = self._keys("mechanism a", "mechanism b")
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(mechanism="mechanism a"),
                    candidate(mechanism="mechanism b"),
                ]
            }
        )
        auditor = FakeAuditor(
            [
                self._decision(keys["mechanism a"], refs=["source_claim:ev-1"]),
                self._decision(
                    keys["mechanism b"],
                    verdict="contradicted",
                    refs=["source_claim:ev-1"],
                    rationale="excerpt contradicts the claim",
                ),
            ]
        )
        result = run([item], runner, roles=("fundamental",), auditor=auditor)
        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(result.ranked[0].candidate.mechanism, "mechanism a")
        self.assertEqual(len(result.audit_decisions), 2)
        verdicts = {
            decision.candidate_key: decision.verdict
            for decision in result.audit_decisions
        }
        self.assertIs(verdicts[keys["mechanism b"]], CitationVerdict.CONTRADICTED)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("verdict:contradicted", result.rejected[0].reason)

    def test_auditor_none_preserves_pure_behavior(self):
        item = evidence()
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(mechanism="mechanism a"),
                    candidate(mechanism="mechanism b"),
                ]
            }
        )
        result = run([item], runner, roles=("fundamental",))
        self.assertEqual(len(result.ranked), 2)
        self.assertEqual(result.audit_decisions, ())

    def test_missing_audit_decision_rejects_candidate(self):
        item = evidence()
        keys = self._keys("mechanism a", "mechanism b")
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(mechanism="mechanism a"),
                    candidate(mechanism="mechanism b"),
                ]
            }
        )
        auditor = FakeAuditor(
            [self._decision(keys["mechanism a"], refs=["source_claim:ev-1"])]
        )
        result = run([item], runner, roles=("fundamental",), auditor=auditor)
        self.assertEqual(len(result.ranked), 1)
        self.assertEqual(len(result.audit_decisions), 1)
        self.assertEqual(len(result.rejected), 1)
        self.assertIn("missing decision", result.rejected[0].reason)

    def test_malformed_audit_decisions_reject_batch(self):
        item = evidence()
        keys = self._keys("mechanism a")
        runner = FakeRunner({"fundamental": [candidate(mechanism="mechanism a")]})
        bad_decisions = [
            self._decision(keys["mechanism a"], verdict="maybe"),
            {"candidate_key": keys["mechanism a"], "verdict": "entailed"},
            self._decision("some-unknown-key"),
            self._decision(keys["mechanism a"], refs=["source_claim:ghost"]),
            self._decision(keys["mechanism a"], unsupported=["x"] * 12),
        ]
        for bad in bad_decisions:
            result = run(
                [item],
                runner,
                roles=("fundamental",),
                auditor=FakeAuditor([bad]),
            )
            self.assertEqual(len(result.ranked), 0, bad)
            self.assertEqual(len(result.audit_decisions), 0)
            self.assertIn("citation audit failed", result.rejected[0].reason)

    def test_auditor_exception_rejects_batch(self):
        item = evidence()
        runner = FakeRunner({"fundamental": [candidate(mechanism="mechanism a")]})
        result = run(
            [item],
            runner,
            roles=("fundamental",),
            auditor=FakeAuditor(fail=True),
        )
        self.assertEqual(len(result.ranked), 0)
        self.assertEqual(len(result.audit_decisions), 0)
        self.assertIn("citation audit failed", result.rejected[0].reason)

    def test_large_audit_is_partitioned_and_failure_isolated(self):
        class PartitionedAuditor:
            def __init__(self):
                self.calls = []

            def audit(self, *, candidates, evidence):
                self.calls.append({"candidates": candidates, "evidence": evidence})
                if len(self.calls) == 2:
                    raise RuntimeError("second batch unavailable")
                return [
                    {
                        "candidate_key": item["candidate_key"],
                        "verdict": "entailed",
                        "cited_refs": item["evidence_refs"],
                        "unsupported_claims": [],
                        "rationale": "The cited excerpt supports the claim.",
                    }
                    for item in candidates
                ]

        outputs = [
            candidate(mechanism=f"capacity path {chr(ord('a') + index)}")
            for index in range(17)
        ]
        auditor = PartitionedAuditor()
        result = run(
            [evidence()],
            FakeRunner({"fundamental": outputs}),
            roles=("fundamental",),
            auditor=auditor,
            max_per_role=32,
        )

        self.assertEqual(
            [len(call["candidates"]) for call in auditor.calls],
            [16, 1],
        )
        self.assertEqual(len(result.audit_decisions), 16)
        self.assertEqual(len(result.ranked), 16)
        self.assertTrue(
            any("second batch unavailable" in item.reason for item in result.rejected)
        )

    def test_auditor_output_order_does_not_change_results(self):
        item = evidence()
        keys = self._keys("mechanism a", "mechanism b")
        runner = FakeRunner(
            {
                "fundamental": [
                    candidate(mechanism="mechanism a"),
                    candidate(mechanism="mechanism b"),
                ]
            }
        )
        forward = [
            self._decision(keys["mechanism a"], refs=["source_claim:ev-1"]),
            self._decision(keys["mechanism b"], verdict="unsupported"),
        ]
        first = run(
            [item],
            runner,
            roles=("fundamental",),
            auditor=FakeAuditor(forward),
        )
        second = run(
            [item],
            runner,
            roles=("fundamental",),
            auditor=FakeAuditor(list(reversed(forward))),
        )
        self.assertEqual(
            [entry.candidate.candidate_key for entry in first.ranked],
            [entry.candidate.candidate_key for entry in second.ranked],
        )
        self.assertEqual(
            [entry.verdict for entry in first.audit_decisions],
            [entry.verdict for entry in second.audit_decisions],
        )

    def test_role_runner_cannot_be_the_auditor(self):
        item = evidence()
        runner = FakeRunner({"fundamental": [candidate()]})
        with self.assertRaises(ValueError):
            run([item], runner, roles=("fundamental",), auditor=runner)

    def test_auditor_receives_validated_candidates_and_catalog(self):
        item = evidence()
        auditor = FakeAuditor([])
        run(
            [item],
            FakeRunner({"fundamental": [candidate()]}),
            roles=("fundamental",),
            auditor=auditor,
        )
        self.assertEqual(len(auditor.calls), 1)
        payload = auditor.calls[0]["candidates"][0]
        self.assertEqual(payload["subject"], "nvidia")
        self.assertIn("source_claim:ev-1", auditor.calls[0]["evidence"])
        self.assertIn("evidence_refs", payload)


class CitationAuditTests(unittest.TestCase):
    def test_audit_is_independent_and_reports_unknown_refs(self):
        item = evidence()
        catalog = {"source_claim:ev-1": item}
        draft = CandidateDraft(
            role="fundamental",
            index=0,
            claim="claim",
            subject="nvidia",
            instrument="NVDA",
            direction="long",
            horizon="months",
            consensus="consensus",
            variant_perception="variant",
            mechanism="mechanism",
            catalyst="catalyst",
            scenarios=(
                Scenario.create(label="bull", probability=0.3, expected_return=0.4),
                Scenario.create(label="base", probability=0.5, expected_return=0.1),
                Scenario.create(label="bear", probability=0.2, expected_return=-0.3),
            ),
            scenario_paths=("bull path", "base path", "bear path"),
            invalidators=(),
            missing_evidence=(),
            evidence_refs=("source_claim:ghost",),
            confidence=None,
            candidate_key="key",
            content_fingerprint="fingerprint",
            completeness=1.0,
        )
        findings = audit_citations([draft], catalog)
        self.assertEqual(len(findings), 1)
        self.assertIsInstance(findings[0], CitationFinding)
        self.assertEqual(findings[0].unknown_refs, ("source_claim:ghost",))
        self.assertEqual(findings[0].unsafe_refs, ())

    def test_audit_reports_unsafe_evidence(self):
        item = evidence(point_in_time_safe=False)
        catalog = {"source_claim:ev-1": item}
        draft = CandidateDraft(
            role="fundamental",
            index=0,
            claim="claim",
            subject="nvidia",
            instrument="NVDA",
            direction="long",
            horizon="months",
            consensus="consensus",
            variant_perception="variant",
            mechanism="mechanism",
            catalyst="catalyst",
            scenarios=(
                Scenario.create(label="bull", probability=0.3, expected_return=0.4),
                Scenario.create(label="base", probability=0.5, expected_return=0.1),
                Scenario.create(label="bear", probability=0.2, expected_return=-0.3),
            ),
            scenario_paths=("bull path", "base path", "bear path"),
            invalidators=(),
            missing_evidence=(),
            evidence_refs=("source_claim:ev-1",),
            confidence=None,
            candidate_key="key",
            content_fingerprint="fingerprint",
            completeness=1.0,
        )
        findings = audit_citations([draft], catalog)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].unsafe_refs, ("source_claim:ev-1",))
        self.assertEqual(findings[0].unknown_refs, ())


if __name__ == "__main__":
    unittest.main()
