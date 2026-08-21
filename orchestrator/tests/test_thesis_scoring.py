import math
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_intelligence.contracts import EvidenceSignal, Scenario
from thesis_scoring import (
    CORRELATION_DECAY,
    MAX_EVIDENCE,
    MAX_SCENARIOS,
    NeglectScore,
    assess_evidence,
    assess_independence,
    assess_opportunity,
    calculate_neglect,
    canonicalize_evidence,
    catalyst_readiness,
    is_auditable_evidence,
    scenario_valuation,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def signal(**overrides):
    base = {
        "evidence_id": "ev-1",
        "evidence_type": "source_claim",
        "relationship": "supports",
        "source_name": "reuters",
        "content": {"claim": "demand is accelerating"},
        "source_timestamp": NOW,
        # Explicit positive scores + a bounded verbatim excerpt make the
        # helper's signals auditable under ``is_auditable_evidence``; the
        # values equal the old neutral defaults so weight math is unchanged.
        "quality_score": 0.5,
        "entailment_score": 0.5,
        "freshness_score": 0.5,
        "provenance": {"excerpt": "Demand is accelerating across the channel."},
    }
    base.update(overrides)
    return EvidenceSignal.create(**base)


def scenario(**overrides):
    base = {"label": "base", "expected_return": 0.1}
    base.update(overrides)
    return Scenario.create(**base)


class DuplicateAndCorrelationTests(unittest.TestCase):
    def test_duplicated_syndication_scores_once(self):
        repeated = [
            signal(
                evidence_id="wire-1",
                source_name="reuters",
                source_family="wire",
                origin_key="feed-1",
            ),
            signal(
                evidence_id="wire-2",
                source_name="bloomberg",
                source_family="wire",
                origin_key="feed-2",
            ),
            signal(
                evidence_id="wire-3",
                source_name="financial-times",
                source_family="wire",
                origin_key="feed-3",
            ),
        ]
        score = assess_evidence(repeated)
        single = assess_evidence(repeated[:1])
        self.assertEqual(score.evidence_input_count, 3)
        self.assertEqual(score.unique_evidence_count, 1)
        self.assertEqual(score.support_count, 1)
        self.assertEqual(len(score.dropped_duplicate_ids), 2)
        self.assertEqual(score.support_mass, single.support_mass)
        catalog = canonicalize_evidence(repeated)
        self.assertEqual(len(catalog.unique), 1)
        self.assertEqual(sorted(catalog.dropped_duplicate_ids), ["wire-2", "wire-3"])

    def test_identical_evidence_from_different_agents_scores_once(self):
        analyst = signal(
            evidence_id="ev-a",
            source_name="analyst-alpha",
            provenance={"agent": "analyst", "role": "bull"},
        )
        critic = signal(
            evidence_id="ev-b",
            source_name="analyst-beta",
            provenance={"agent": "analyst", "role": "devils-advocate"},
        )
        self.assertEqual(analyst.evidence_fingerprint, critic.evidence_fingerprint)
        score = assess_evidence([analyst, critic])
        self.assertEqual(score.unique_evidence_count, 1)
        self.assertEqual(score.support_mass, assess_evidence([analyst]).support_mass)

    def test_provenance_never_affects_mass(self):
        plain = signal(evidence_id="ev-x", content={"claim": "same claim"})
        adorned = signal(
            evidence_id="ev-y",
            content={"claim": "same claim"},
            provenance={
                "agent": "a",
                "role": "r",
                "model": "m",
                "excerpt": "Demand is accelerating across the channel.",
            },
        )
        left = assess_evidence([plain, adorned])
        right = assess_evidence([adorned, plain])
        self.assertEqual(left.support_mass, right.support_mass)
        self.assertEqual(left.confidence, right.confidence)
        self.assertEqual(left.unique_evidence_count, 1)

    def test_same_source_family_is_capped_with_diminishing_returns(self):
        first = signal(evidence_id="fam-1", content={"claim": "story one"})
        second = signal(evidence_id="fam-2", content={"claim": "story two"})
        both = signal(evidence_id="fam-3", content={"claim": "story three"})
        capped = assess_evidence(
            [
                signal(
                    evidence_id="a",
                    source_family="wire",
                    content={"claim": "story one"},
                ),
                signal(
                    evidence_id="b",
                    source_family="wire",
                    content={"claim": "story two"},
                ),
            ]
        )
        independent = assess_evidence([first, second])
        weight = 0.5 * 0.5 * 0.5
        self.assertAlmostEqual(
            capped.support_mass, 1 - math.exp(-weight * (1 + CORRELATION_DECAY))
        )
        self.assertAlmostEqual(independent.support_mass, 1 - math.exp(-weight * 2))
        self.assertLess(capped.support_mass, independent.support_mass)
        self.assertAlmostEqual(
            assess_evidence([first, second, both]).support_mass,
            1 - math.exp(-weight * 3),
        )

    def test_independence_key_caps_correlated_evidence(self):
        capped = assess_evidence(
            [
                signal(
                    evidence_id="k-1",
                    independence_key="press-release-42",
                    source_family="reuters",
                    content={"claim": "story one"},
                ),
                signal(
                    evidence_id="k-2",
                    independence_key="press-release-42",
                    source_family="bloomberg",
                    content={"claim": "story two"},
                ),
            ]
        )
        weight = 0.5 * 0.5 * 0.5
        self.assertAlmostEqual(
            capped.support_mass, 1 - math.exp(-weight * (1 + CORRELATION_DECAY))
        )
        self.assertEqual(capped.independent_group_count, 1)

    def test_independence_key_takes_precedence_over_family(self):
        assessment = assess_independence(
            [
                signal(
                    evidence_id="a",
                    independence_key="pr-1",
                    source_family="wire",
                    content={"claim": "one"},
                ),
                signal(
                    evidence_id="b",
                    independence_key="pr-1",
                    source_family="wire",
                    content={"claim": "two"},
                ),
                signal(
                    evidence_id="c",
                    source_family="wire",
                    content={"claim": "three"},
                ),
            ]
        )
        self.assertEqual(assessment.group_count, 2)
        kinds = {group.group_kind for group in assessment.groups}
        self.assertEqual(kinds, {"independence_key", "source_family"})

    def test_independent_primary_sources_raise_support_with_diminishing_returns(self):
        weight = 0.5 * 0.5 * 0.5
        one = assess_evidence([signal(evidence_id="s1", content={"claim": "c1"})])
        two = assess_evidence(
            [
                signal(evidence_id="s1", content={"claim": "c1"}),
                signal(evidence_id="s2", content={"claim": "c2"}),
            ]
        )
        three = assess_evidence(
            [
                signal(evidence_id="s1", content={"claim": "c1"}),
                signal(evidence_id="s2", content={"claim": "c2"}),
                signal(evidence_id="s3", content={"claim": "c3"}),
            ]
        )
        mass_one = 1 - math.exp(-weight)
        mass_two = 1 - math.exp(-2 * weight)
        mass_three = 1 - math.exp(-3 * weight)
        self.assertAlmostEqual(one.support_mass, mass_one)
        self.assertAlmostEqual(two.support_mass, mass_two)
        self.assertAlmostEqual(three.support_mass, mass_three)
        delta_two = two.support_mass - one.support_mass
        delta_three = three.support_mass - two.support_mass
        self.assertGreater(delta_two, 0)
        self.assertGreater(delta_two, delta_three)

    def test_evaluation_is_order_independent(self):
        evidence = [
            signal(
                evidence_id="a",
                source_family="wire",
                content={"claim": "one"},
            ),
            signal(
                evidence_id="b",
                source_family="wire",
                content={"claim": "two"},
            ),
            signal(
                evidence_id="c",
                relationship="contradicts",
                content={"claim": "three"},
            ),
            signal(
                evidence_id="d",
                evidence_type="filing_delta",
                relationship="context",
                content={"claim": "four"},
            ),
            signal(
                evidence_id="e",
                evidence_type="macro_release",
                content={"claim": "five"},
            ),
        ]
        forward = assess_evidence(evidence)
        backward = assess_evidence(list(reversed(evidence)))
        self.assertEqual(forward.support_mass, backward.support_mass)
        self.assertEqual(forward.contradiction_mass, backward.contradiction_mass)
        self.assertEqual(forward.confidence, backward.confidence)
        self.assertEqual(forward.diversity, backward.diversity)
        self.assertEqual(
            set(forward.support_evidence_ids),
            set(backward.support_evidence_ids),
        )

    def test_dedupe_tie_break_is_order_independent(self):
        forward = [
            signal(evidence_id="z-dup", content={"claim": "same claim"}),
            signal(evidence_id="a-dup", content={"claim": "same claim"}),
        ]
        backward = list(reversed(forward))
        left = canonicalize_evidence(forward)
        right = canonicalize_evidence(backward)
        self.assertEqual(left.unique[0].evidence_id, "a-dup")
        self.assertEqual(left.unique[0].evidence_id, right.unique[0].evidence_id)
        self.assertEqual(len(left.unique), 1)
        self.assertEqual(len(right.unique), 1)

    def test_bounded_evidence_input(self):
        evidence = [
            signal(
                evidence_id=f"bulk-{index}",
                content={"claim": f"bulk {index}"},
            )
            for index in range(MAX_EVIDENCE + 50)
        ]
        score = assess_evidence(evidence)
        self.assertEqual(score.evidence_input_count, MAX_EVIDENCE)
        self.assertLessEqual(score.unique_evidence_count, MAX_EVIDENCE)


class ContradictionAndConfidenceTests(unittest.TestCase):
    def test_contradictions_remain_visible(self):
        score = assess_evidence(
            [
                signal(evidence_id="sup-1", content={"claim": "bull one"}),
                signal(evidence_id="sup-2", content={"claim": "bull two"}),
                signal(
                    evidence_id="con-1",
                    relationship="contradicts",
                    content={"claim": "bear one"},
                ),
            ]
        )
        self.assertGreater(score.support_mass, 0)
        self.assertGreater(score.contradiction_mass, 0)
        self.assertEqual(score.contradiction_evidence_ids, ("con-1",))
        self.assertEqual(set(score.support_evidence_ids), {"sup-1", "sup-2"})
        self.assertEqual(score.contradiction_count, 1)
        self.assertEqual(score.support_count, 2)
        without_contradiction = assess_evidence(
            [
                signal(evidence_id="sup-1", content={"claim": "bull one"}),
                signal(evidence_id="sup-2", content={"claim": "bull two"}),
            ]
        )
        self.assertIsNotNone(score.confidence)
        self.assertLess(score.confidence, without_contradiction.confidence)

    def test_contradiction_mass_caps_confidence(self):
        pure_contradiction = assess_evidence(
            [
                signal(
                    evidence_id="con-1",
                    relationship="contradicts",
                    content={"claim": "bear one"},
                ),
                signal(
                    evidence_id="con-2",
                    relationship="contradicts",
                    content={"claim": "bear two"},
                ),
            ]
        )
        self.assertIsNotNone(pure_contradiction.confidence)
        self.assertLess(pure_contradiction.confidence, 0.5)

    def test_context_only_evidence_has_no_confidence(self):
        score = assess_evidence(
            [
                signal(
                    evidence_id="ctx-1",
                    relationship="context",
                    content={"claim": "background"},
                )
            ]
        )
        self.assertIsNone(score.confidence)
        self.assertEqual(score.support_mass, 0.0)
        self.assertEqual(score.contradiction_mass, 0.0)
        self.assertEqual(score.context_count, 1)

    def test_invalidation_accepted_without_directional_mass(self):
        score = assess_evidence(
            [
                signal(
                    evidence_id="inv-1",
                    relationship="invalidation",
                    content={"claim": "mechanism disproven"},
                ),
                signal(
                    evidence_id="inv-2",
                    relationship="invalidation",
                    content={"claim": "outcome contradicts thesis"},
                ),
            ]
        )
        self.assertEqual(score.support_mass, 0.0)
        self.assertEqual(score.contradiction_mass, 0.0)
        self.assertIsNone(score.confidence)
        self.assertEqual(score.invalidation_count, 2)
        self.assertEqual(score.context_count, 0)
        self.assertEqual(score.support_count, 0)
        self.assertEqual(score.contradiction_count, 0)
        self.assertEqual(score.unique_evidence_count, 2)

    def test_invalidation_adds_no_support_or_contradiction_mass(self):
        directional = assess_evidence(
            [signal(evidence_id="sup-1", content={"claim": "bull one"})]
        )
        with_invalidation = assess_evidence(
            [
                signal(evidence_id="sup-1", content={"claim": "bull one"}),
                signal(
                    evidence_id="inv-1",
                    relationship="invalidation",
                    content={"claim": "mechanism disproven"},
                ),
            ]
        )
        self.assertEqual(with_invalidation.support_mass, directional.support_mass)
        self.assertEqual(
            with_invalidation.contradiction_mass,
            directional.contradiction_mass,
        )
        self.assertEqual(with_invalidation.invalidation_count, 1)
        self.assertEqual(with_invalidation.support_evidence_ids, ("sup-1",))

    def test_unscored_rows_contribute_nothing(self):
        # Quality and entailment are never defaulted for contribution: a row
        # without explicit positive scores is not auditable, so it adds no
        # mass and cannot make a thesis directionally scored.
        unscored = signal(
            evidence_id="m-1",
            content={"claim": "claim"},
            quality_score=None,
            entailment_score=None,
            freshness_score=None,
        )
        scored = signal(
            evidence_id="e-1",
            content={"claim": "claim"},
            quality_score=0.5,
            entailment_score=0.5,
            freshness_score=0.5,
            effective_weight=1.0,
        )
        score_unscored = assess_evidence([unscored])
        score_scored = assess_evidence([scored])
        self.assertEqual(score_unscored.support_mass, 0.0)
        self.assertEqual(score_unscored.support_count, 0)
        self.assertIsNone(score_unscored.confidence)
        self.assertGreater(score_scored.support_mass, 0.0)
        self.assertEqual(score_scored.support_count, 1)
        self.assertIsNotNone(score_scored.confidence)
        self.assertEqual(score_scored.missing_quality_count, 0)

    def test_zero_quality_placeholder_rows_contribute_no_mass(self):
        # A stored 0.0 is the persistence default for unscored placeholder
        # rows (empty FRED/story rows). They normalize to unknown inside
        # ``EvidenceSignal`` and fail the auditable predicate: they stay
        # historical/context and never raise evidence strength.
        zero_scored = signal(
            evidence_id="z-1",
            content={"claim": "claim"},
            quality_score=0.0,
            entailment_score=0.0,
            freshness_score=0.0,
            effective_weight=0.0,
        )
        neutral = assess_evidence([zero_scored])
        self.assertEqual(neutral.support_mass, 0.0)
        self.assertEqual(neutral.support_count, 0)
        self.assertEqual(neutral.context_count, 1)
        self.assertIsNone(neutral.confidence)

    def test_confidence_rises_with_quality_freshness_and_entailment(self):
        weak = assess_evidence([signal(evidence_id="w", content={"claim": "claim"})])
        strong = assess_evidence(
            [
                signal(
                    evidence_id="s",
                    content={"claim": "claim"},
                    quality_score=0.9,
                    entailment_score=0.9,
                    freshness_score=0.9,
                )
            ]
        )
        self.assertGreater(strong.confidence, weak.confidence)

    def test_diversity_rises_with_independent_groups_and_modalities(self):
        same_modality = assess_evidence(
            [
                signal(evidence_id="a", content={"claim": "one"}),
                signal(evidence_id="b", content={"claim": "two"}),
            ]
        )
        cross_modal = assess_evidence(
            [
                signal(
                    evidence_id="a",
                    evidence_type="macro_release",
                    content={"claim": "one"},
                ),
                signal(
                    evidence_id="b",
                    evidence_type="filing_delta",
                    content={"claim": "two"},
                ),
            ]
        )
        self.assertGreater(cross_modal.diversity, same_modality.diversity)


class AuditableEvidenceTests(unittest.TestCase):
    """The auditable-evidence predicate gates scoring contribution the same
    way persistence and rank eligibility gate promotion."""

    def test_predicate_requires_excerpt_positive_quality_and_entailment(self):
        auditable = signal(
            evidence_id="ok-1",
            quality_score=0.8,
            entailment_score=0.7,
            provenance={"excerpt": "Verbatim disclosed cost trend."},
        )
        self.assertTrue(is_auditable_evidence(auditable))
        self.assertTrue(is_auditable_evidence(auditable, allow_structured=True))
        self.assertFalse(
            is_auditable_evidence(
                signal(
                    evidence_id="no-excerpt",
                    quality_score=0.8,
                    entailment_score=0.7,
                    provenance={},
                )
            )
        )
        self.assertFalse(
            is_auditable_evidence(
                signal(
                    evidence_id="zero-quality",
                    quality_score=0.0,
                    entailment_score=0.7,
                )
            )
        )
        self.assertFalse(
            is_auditable_evidence(
                signal(
                    evidence_id="no-entailment",
                    quality_score=0.8,
                    entailment_score=0.0,
                )
            )
        )
        self.assertFalse(
            is_auditable_evidence(
                signal(
                    evidence_id="blank-excerpt",
                    quality_score=0.8,
                    entailment_score=0.7,
                    provenance={"excerpt": "   "},
                )
            )
        )

    def test_structured_observation_payload_audits_contradictions_only(self):
        structured = signal(
            evidence_id="con-structured",
            relationship="contradicts",
            quality_score=0.8,
            entailment_score=0.8,
            provenance={"structured_fields": {"series_id": "FRED_X", "value": 42.0}},
        )
        self.assertTrue(is_auditable_evidence(structured, allow_structured=True))
        self.assertFalse(is_auditable_evidence(structured))
        # Support without a narrative excerpt is never auditable, even with
        # a structured payload: support needs checkable verbatim content.
        support = signal(
            evidence_id="sup-structured",
            relationship="supports",
            quality_score=0.8,
            entailment_score=0.8,
            provenance={"structured_fields": {"series_id": "FRED_X", "value": 42.0}},
        )
        self.assertFalse(is_auditable_evidence(support, allow_structured=True))

    def test_null_excerpt_rows_never_add_support_or_contradiction_mass(self):
        placeholder = signal(
            evidence_id="ph-1",
            relationship="supports",
            quality_score=0.8,
            entailment_score=0.8,
            provenance={},
        )
        score = assess_evidence([placeholder])
        self.assertEqual(score.support_mass, 0.0)
        self.assertEqual(score.support_count, 0)
        self.assertEqual(score.context_count, 1)
        self.assertIsNone(score.confidence)

    def test_audited_shape_placeholders_contribute_no_contradiction_mass(self):
        # The top audited shape: one valid support plus two null-excerpt /
        # zero-quality contradiction placeholders (empty FRED/story rows).
        support = signal(
            evidence_id="sup-audited",
            content={"claim": "margin expansion is durable"},
            quality_score=0.9,
            entailment_score=0.9,
            freshness_score=0.8,
        )
        placeholder_a = signal(
            evidence_id="ph-a",
            relationship="contradicts",
            content={"claim": "fred placeholder"},
            quality_score=0.0,
            entailment_score=0.0,
            provenance={},
        )
        placeholder_b = signal(
            evidence_id="ph-b",
            relationship="contradicts",
            content={"claim": "story placeholder"},
            quality_score=0.0,
            entailment_score=0.0,
            provenance={},
        )
        score = assess_evidence([support, placeholder_a, placeholder_b])
        self.assertEqual(score.support_count, 1)
        self.assertGreater(score.support_mass, 0.0)
        self.assertEqual(score.contradiction_count, 0)
        self.assertEqual(score.contradiction_mass, 0.0)
        self.assertEqual(score.contradiction_evidence_ids, ())
        # The placeholders stay historical/context, never directional.
        self.assertEqual(score.context_count, 2)
        self.assertIsNotNone(score.confidence)
        # On their own the placeholders cannot produce confidence at all.
        alone = assess_evidence([placeholder_a, placeholder_b])
        self.assertIsNone(alone.confidence)
        self.assertEqual(alone.contradiction_mass, 0.0)

    def test_structured_observation_contradiction_contributes(self):
        contradiction = signal(
            evidence_id="con-structured",
            relationship="contradicts",
            content={"claim": "bear observation"},
            quality_score=0.8,
            entailment_score=0.8,
            provenance={"structured_fields": {"series_id": "FRED_X", "value": 42.0}},
        )
        score = assess_evidence([contradiction])
        self.assertEqual(score.contradiction_count, 1)
        self.assertGreater(score.contradiction_mass, 0.0)
        self.assertEqual(score.contradiction_evidence_ids, ("con-structured",))
        self.assertIsNotNone(score.confidence)

    def test_duplicate_excerpts_never_double_count(self):
        # Identical content (same fingerprint) repeated through any number
        # of cited excerpts is one evidence item and scores exactly once.
        first = signal(
            evidence_id="dup-1",
            content={"claim": "the same disclosed claim"},
            quality_score=0.9,
            entailment_score=0.9,
        )
        second = signal(
            evidence_id="dup-2",
            content={"claim": "the same disclosed claim"},
            quality_score=0.9,
            entailment_score=0.9,
        )
        repeated = assess_evidence([first, second])
        single = assess_evidence([first])
        self.assertEqual(repeated.support_count, 1)
        self.assertEqual(repeated.unique_evidence_count, 1)
        self.assertEqual(len(repeated.dropped_duplicate_ids), 1)
        self.assertEqual(repeated.support_mass, single.support_mass)


class InvalidInputTests(unittest.TestCase):
    def test_nan_and_infinite_scores_rejected(self):
        with self.assertRaises(ValueError):
            signal(quality_score=float("nan"))
        with self.assertRaises(ValueError):
            signal(entailment_score=float("inf"))
        with self.assertRaises(ValueError):
            signal(freshness_score=float("-inf"))
        with self.assertRaises(ValueError):
            signal(effective_weight=1.5)
        with self.assertRaises(ValueError):
            signal(quality_score="0.5")
        with self.assertRaises(ValueError):
            signal(relationship="speculation")
        with self.assertRaises(ValueError):
            signal(evidence_id="bad id with spaces!")

    def test_fingerprint_required_and_consistent(self):
        with self.assertRaises(ValueError):
            EvidenceSignal.create(
                evidence_id="ev-1",
                source_name="reuters",
                source_timestamp=NOW,
            )
        with self.assertRaises(ValueError):
            signal(evidence_fingerprint="not-a-hash")
        with self.assertRaises(ValueError):
            signal(
                evidence_fingerprint="0" * 64,
                content={"claim": "something else"},
            )

    def test_scenario_nan_and_infinite_rejected(self):
        with self.assertRaises(ValueError):
            scenario(expected_return=float("inf"))
        with self.assertRaises(ValueError):
            scenario(expected_return=float("nan"))
        with self.assertRaises(ValueError):
            scenario(expected_return=1000.0)
        with self.assertRaises(ValueError):
            scenario(probability=float("nan"))
        with self.assertRaises(ValueError):
            scenario(probability=1.5)
        with self.assertRaises(ValueError):
            scenario(probability=-0.1)

    def test_component_inputs_validated(self):
        with self.assertRaises(ValueError):
            assess_opportunity(liquidity=float("nan"))
        with self.assertRaises(ValueError):
            assess_opportunity(neglect=2.0)
        with self.assertRaises(ValueError):
            calculate_neglect(attention=float("inf"))
        with self.assertRaises(ValueError):
            calculate_neglect(crowding="high")

    def test_duplicate_scenario_labels_rejected(self):
        with self.assertRaises(ValueError):
            scenario_valuation(
                [
                    scenario(label="same", probability=0.5, expected_return=0.1),
                    scenario(label="same", probability=0.5, expected_return=0.2),
                ]
            )


class ScenarioValuationTests(unittest.TestCase):
    def test_probabilities_not_summing_to_one_are_not_renormalized(self):
        valuation = scenario_valuation(
            [
                scenario(label="base", probability=0.3, expected_return=0.20),
                scenario(label="down", probability=0.3, expected_return=-0.50),
            ]
        )
        self.assertAlmostEqual(valuation.probability_sum, 0.6)
        self.assertFalse(valuation.probabilities_sum_to_one)
        self.assertAlmostEqual(valuation.expected_value, -0.09)
        self.assertAlmostEqual(valuation.expected_shortfall, 0.15)
        self.assertEqual(valuation.expected_values["base"], 0.06)
        self.assertEqual(valuation.expected_values["down"], -0.15)

    def test_cost_reduces_net_expected_return(self):
        valuation = scenario_valuation(
            [scenario(label="base", probability=0.5, expected_return=0.20)],
            cost=0.05,
        )
        self.assertAlmostEqual(valuation.expected_value, 0.05)

    def test_zero_downside_shortfall(self):
        valuation = scenario_valuation(
            [
                scenario(label="base", probability=0.6, expected_return=0.10),
                scenario(label="up", probability=0.4, expected_return=0.30),
            ]
        )
        self.assertEqual(valuation.expected_shortfall, 0.0)
        self.assertAlmostEqual(valuation.expected_value, 0.18)

    def test_missing_probability_never_defaulted_to_conviction(self):
        valuation = scenario_valuation(
            [
                scenario(label="priced", probability=0.5, expected_return=0.10),
                scenario(label="unknown", probability=None, expected_return=0.90),
            ]
        )
        self.assertAlmostEqual(valuation.expected_value, 0.05)
        self.assertEqual(valuation.missing_probability_count, 1)
        self.assertEqual(valuation.missing_probability_labels, ("unknown",))
        self.assertNotIn("unknown", valuation.expected_values)
        self.assertEqual(valuation.ranks["priced"], 1)
        self.assertEqual(valuation.ranks["unknown"], 2)

    def test_rank_is_deterministic_and_finite(self):
        valuation = scenario_valuation(
            [
                scenario(label="a", probability=0.5, expected_return=0.2),
                scenario(label="b", probability=0.2, expected_return=0.6),
                scenario(label="c", probability=0.5, expected_return=0.2),
                scenario(label="d", probability=0.5, expected_return=-0.4),
                scenario(label="e", probability=None, expected_return=0.3),
            ]
        )
        self.assertEqual(valuation.ranks, {"b": 1, "a": 2, "c": 3, "d": 4, "e": 5})
        self.assertAlmostEqual(valuation.expected_value, 0.12)
        self.assertTrue(math.isfinite(valuation.expected_value))
        again = scenario_valuation(
            [
                scenario(label="a", probability=0.5, expected_return=0.2),
                scenario(label="b", probability=0.2, expected_return=0.6),
                scenario(label="c", probability=0.5, expected_return=0.2),
                scenario(label="d", probability=0.5, expected_return=-0.4),
                scenario(label="e", probability=None, expected_return=0.3),
            ]
        )
        self.assertEqual(valuation.ranks, again.ranks)

    def test_zero_probability_is_a_real_scenario_not_missing(self):
        # Unlike evidence scores, probability 0.0 means an impossible
        # scenario and is preserved as priced, contributing nothing.
        valuation = scenario_valuation(
            [
                scenario(label="never", probability=0.0, expected_return=-0.9),
                scenario(label="base", probability=0.5, expected_return=0.2),
            ]
        )
        self.assertEqual(valuation.priced_scenario_count, 2)
        self.assertEqual(valuation.missing_probability_count, 0)
        self.assertEqual(valuation.expected_values["never"], 0.0)
        self.assertAlmostEqual(valuation.expected_value, 0.10)

    def test_bounded_scenario_input(self):
        valuation = scenario_valuation(
            [
                scenario(label=f"s-{index}", probability=0.01, expected_return=0.01)
                for index in range(MAX_SCENARIOS + 10)
            ]
        )
        self.assertEqual(valuation.scenario_count, MAX_SCENARIOS)
        self.assertTrue(math.isfinite(valuation.expected_value))


class OpportunityGateTests(unittest.TestCase):
    def test_missing_catalyst_blocks_opportunity(self):
        score = assess_opportunity(
            evidence_strength=0.8,
            confidence=0.7,
            neglect=0.5,
            catalyst_ready=None,
            liquidity=0.7,
            downside=0.2,
        )
        self.assertEqual(score.opportunity, 0.0)
        self.assertIn("catalyst", score.blocked_by)
        self.assertIn("catalyst", score.missing)
        self.assertFalse(score.gates["catalyst"])
        self.assertTrue(score.gates["evidence"])

    def test_opportunity_blend_when_all_gates_pass(self):
        score = assess_opportunity(
            evidence_strength=0.8,
            confidence=0.7,
            neglect=0.5,
            catalyst_ready=0.6,
            liquidity=0.7,
            downside=0.2,
        )
        expected = (
            0.30 * 0.8
            + 0.25 * 0.7
            + 0.15 * 0.5
            + 0.10 * 0.6
            + 0.10 * 0.7
            + 0.10 * (1.0 - 0.2)
        )
        self.assertAlmostEqual(score.opportunity, expected)
        self.assertEqual(score.blocked_by, ())
        self.assertEqual(score.missing, ())

    def test_weak_evidence_blocks_opportunity(self):
        score = assess_opportunity(
            evidence_strength=0.1,
            confidence=0.7,
            neglect=0.5,
            catalyst_ready=0.6,
            liquidity=0.7,
            downside=0.2,
        )
        self.assertEqual(score.opportunity, 0.0)
        self.assertIn("evidence", score.blocked_by)

    def test_high_downside_blocks_opportunity(self):
        score = assess_opportunity(
            evidence_strength=0.8,
            confidence=0.7,
            neglect=0.5,
            catalyst_ready=0.6,
            liquidity=0.7,
            downside=0.9,
        )
        self.assertEqual(score.opportunity, 0.0)
        self.assertIn("downside", score.blocked_by)

    def test_missing_liquidity_reported_explicitly(self):
        score = assess_opportunity(
            evidence_strength=0.8,
            confidence=0.7,
            neglect=0.5,
            catalyst_ready=0.6,
            liquidity=None,
            downside=0.2,
        )
        self.assertEqual(score.opportunity, 0.0)
        self.assertIn("liquidity", score.missing)
        self.assertIn("liquidity", score.blocked_by)


class CatalystAndNeglectTests(unittest.TestCase):
    def catalyst(self, **overrides):
        base = {"description": "earnings release"}
        base.update(overrides)
        from thesis_scoring import CatalystSignal

        return CatalystSignal.create(**base)

    def test_empty_catalyst_set_is_missing(self):
        score = catalyst_readiness([], as_of=NOW)
        self.assertIsNone(score.readiness)
        self.assertEqual(score.missing, ("catalyst",))
        self.assertEqual(score.catalyst_count, 0)

    def test_catalyst_readiness_states(self):
        confirmed = self.catalyst(state="confirmed")
        self.assertEqual(catalyst_readiness([confirmed], as_of=NOW).readiness, 1.0)
        missed = self.catalyst(state="missed")
        self.assertEqual(catalyst_readiness([missed], as_of=NOW).readiness, 0.0)
        expired = self.catalyst(state="expired")
        self.assertEqual(catalyst_readiness([expired], as_of=NOW).readiness, 0.0)
        near = self.catalyst(expected_at=NOW + timedelta(days=45))
        self.assertAlmostEqual(catalyst_readiness([near], as_of=NOW).readiness, 0.5)
        overdue = self.catalyst(expected_at=NOW - timedelta(days=1))
        self.assertEqual(catalyst_readiness([overdue], as_of=NOW).readiness, 1.0)
        far = self.catalyst(expected_at=NOW + timedelta(days=365))
        self.assertEqual(catalyst_readiness([far], as_of=NOW).readiness, 0.0)
        mixed = catalyst_readiness([confirmed, missed], as_of=NOW)
        self.assertEqual(mixed.readiness, 0.5)

    def test_pending_catalyst_without_date_is_neutral_and_missing(self):
        undated = self.catalyst(state="pending")
        score = catalyst_readiness([undated], as_of=NOW)
        self.assertEqual(score.readiness, 0.5)
        self.assertEqual(score.missing, ("catalyst_expected_at",))

    def test_invalid_catalyst_rejected(self):
        with self.assertRaises(ValueError):
            self.catalyst(state="maybe")

    def test_neglect_missing_states(self):
        score = calculate_neglect()
        self.assertIsNone(score.neglect)
        self.assertEqual(score.missing, ("attention", "crowding"))
        self.assertIsInstance(score, NeglectScore)

    def test_neglect_blend(self):
        score = calculate_neglect(attention=0.2, crowding=0.8)
        self.assertAlmostEqual(score.neglect, 0.5)
        attention_only = calculate_neglect(attention=0.3)
        self.assertAlmostEqual(attention_only.neglect, 0.7)
        self.assertEqual(attention_only.missing, ("crowding",))
        crowding_only = calculate_neglect(crowding=0.1)
        self.assertAlmostEqual(crowding_only.neglect, 0.9)


if __name__ == "__main__":
    unittest.main()
