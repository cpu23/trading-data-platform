import random
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opportunity_ranking import (
    OpportunityAssessment,
    assess_opportunity,
    rank_opportunities,
)
from research_intelligence.contracts import EvidenceSignal, Scenario
from thesis_scoring import CatalystSignal

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def signal(**overrides):
    base = {
        "evidence_id": "ev-1",
        "evidence_type": "source_claim",
        "relationship": "supports",
        "source_name": "reuters",
        "content": {"claim": "demand is accelerating"},
        "source_timestamp": NOW,
        "quality_score": 1.0,
        "entailment_score": 1.0,
        "freshness_score": 1.0,
        "effective_weight": 1.0,
        "provenance": {"excerpt": "Demand is accelerating across the channel."},
    }
    base.update(overrides)
    return EvidenceSignal.create(**base)


def scenario(**overrides):
    base = {"label": "base", "probability": 1.0, "expected_return": 0.05}
    base.update(overrides)
    return Scenario.create(**base)


def catalyst(**overrides):
    base = {"description": "approval decision", "state": "confirmed"}
    base.update(overrides)
    return CatalystSignal.create(**base)


def assess(**overrides):
    base = {
        "thesis_id": "thesis-1",
        "evidence": [signal()],
        "scenarios": [
            scenario(label="up", probability=0.5, expected_return=0.2),
            scenario(label="down", probability=0.5, expected_return=-0.1),
        ],
        "catalysts": [catalyst()],
        "attention": 0.1,
        "liquidity": 0.8,
        "downside": 0.2,
        "cost": 0.0,
        "as_of": NOW,
    }
    base.update(overrides)
    return assess_opportunity(**base)


class CostAndEvTests(unittest.TestCase):
    def test_expected_value_is_net_of_transaction_cost(self):
        free = assess(thesis_id="t-1")
        paid = assess(thesis_id="t-2", cost=0.02)
        self.assertAlmostEqual(free.expected_value, 0.05)
        self.assertAlmostEqual(paid.expected_value, 0.03)
        self.assertEqual(paid.transaction_cost, 0.02)
        self.assertAlmostEqual(paid.expected_value, free.expected_value - 0.02)
        # cost never touches the gated score or confidence
        self.assertEqual(paid.score, free.score)
        self.assertEqual(paid.confidence, free.confidence)

    def test_costs_reduce_rank_within_eligible_tier(self):
        free = assess(thesis_id="t-1")
        paid = assess(thesis_id="t-2", cost=0.02)
        ranked = rank_opportunities([paid, free])
        self.assertEqual([r.assessment.thesis_id for r in ranked], ["t-1", "t-2"])
        self.assertEqual(ranked[0].position, 1)
        self.assertEqual(ranked[1].position, 2)


class ProbabilityTests(unittest.TestCase):
    def test_probabilities_never_renormalized(self):
        partial = assess(
            thesis_id="t-1",
            scenarios=[
                scenario(label="up", probability=0.3, expected_return=0.2),
                scenario(label="down", probability=0.3, expected_return=-0.1),
            ],
        )
        self.assertAlmostEqual(partial.probability_sum, 0.6)
        self.assertAlmostEqual(partial.missing_probability_mass, 0.4)
        self.assertAlmostEqual(partial.expected_value, 0.03)
        self.assertEqual(partial.missing_probability_count, 0)

        overpriced = assess(
            thesis_id="t-2",
            scenarios=[
                scenario(label="up", probability=0.6, expected_return=0.2),
                scenario(label="down", probability=0.6, expected_return=-0.1),
            ],
        )
        self.assertAlmostEqual(overpriced.probability_sum, 1.2)
        self.assertAlmostEqual(overpriced.missing_probability_mass, -0.2)
        self.assertAlmostEqual(overpriced.expected_value, 0.06)

    def test_missing_probability_reported_and_blocks_eligibility(self):
        unpriced = assess(
            thesis_id="t-1",
            scenarios=[
                scenario(label="up", probability=0.5, expected_return=0.2),
                scenario(label="tail", probability=None, expected_return=-0.4),
            ],
        )
        self.assertEqual(unpriced.missing_probability_count, 1)
        self.assertEqual(unpriced.missing_probability_labels, ("tail",))
        self.assertAlmostEqual(unpriced.probability_sum, 0.5)
        self.assertAlmostEqual(unpriced.missing_probability_mass, 0.5)
        self.assertAlmostEqual(unpriced.expected_value, 0.1)
        # missing probability is not an opportunity gate: score stands, but
        # the assessment is ineligible for ranking
        self.assertEqual(unpriced.blocked_by, ())
        self.assertGreater(unpriced.score, 0.0)
        self.assertFalse(unpriced.eligible)

        priced = assess(
            thesis_id="t-2",
            scenarios=[
                scenario(label="up", probability=0.5, expected_return=0.2),
                scenario(label="tail", probability=0.5, expected_return=-0.4),
            ],
        )
        self.assertTrue(priced.eligible)
        ranked = rank_opportunities([unpriced, priced])
        self.assertEqual([r.assessment.thesis_id for r in ranked], ["t-2", "t-1"])


class EvidenceAndConfidenceTests(unittest.TestCase):
    def test_contradiction_lowers_confidence(self):
        support = signal(
            evidence_id="ev-a-support",
            independence_key="k1",
            content={"claim": "demand is rising"},
        )
        alone = assess(thesis_id="t-1", evidence=[support])
        contradiction = signal(
            evidence_id="ev-b-contradict",
            independence_key="k1",
            relationship="contradicts",
            content={"claim": "demand is falling"},
        )
        contested = assess(thesis_id="t-2", evidence=[support, contradiction])
        # the supporting leg keeps its full weight; only confidence is damped
        self.assertAlmostEqual(contested.evidence_strength, alone.evidence_strength)
        self.assertIsNotNone(alone.confidence)
        self.assertIsNotNone(contested.confidence)
        self.assertLess(contested.confidence, alone.confidence)
        self.assertEqual(contested.blocked_by, ())
        self.assertTrue(contested.eligible)

    def test_high_evidence_cannot_hide_poor_ev(self):
        strong = assess(
            thesis_id="t-strong",
            evidence=[
                signal(
                    evidence_id="ev-a",
                    content={"claim": "demand is rising"},
                ),
                signal(
                    evidence_id="ev-b",
                    content={"claim": "supply is tightening"},
                ),
            ],
            scenarios=[scenario(label="base", probability=1.0, expected_return=0.02)],
        )
        modest = assess(
            thesis_id="t-modest",
            evidence=[signal()],
            scenarios=[scenario(label="base", probability=1.0, expected_return=0.15)],
        )
        self.assertGreater(strong.evidence_strength, modest.evidence_strength)
        self.assertGreater(strong.score, modest.score)
        self.assertLess(strong.expected_value, modest.expected_value)
        self.assertTrue(strong.eligible)
        self.assertTrue(modest.eligible)
        # expected value dominates the rank tuple: strong evidence does not
        # promote a poor-EV thesis above a good-EV one
        ranked = rank_opportunities([strong, modest])
        self.assertEqual(
            [r.assessment.thesis_id for r in ranked],
            ["t-modest", "t-strong"],
        )


class SeparateComponentTests(unittest.TestCase):
    def test_catalyst_and_neglect_stay_separate(self):
        base = assess(thesis_id="t-base")
        weak_catalyst = assess(
            thesis_id="t-catalyst",
            catalysts=[catalyst(state="pending")],
        )
        crowded = assess(thesis_id="t-crowded", attention=0.8)

        self.assertAlmostEqual(weak_catalyst.catalyst_readiness, 0.5)
        self.assertEqual(weak_catalyst.neglect, base.neglect)
        self.assertAlmostEqual(weak_catalyst.expected_value, base.expected_value)
        self.assertLess(weak_catalyst.score, base.score)

        self.assertAlmostEqual(crowded.neglect, 0.2)
        self.assertEqual(crowded.catalyst_readiness, base.catalyst_readiness)
        self.assertAlmostEqual(crowded.expected_value, base.expected_value)
        self.assertLess(crowded.score, base.score)

    def test_score_is_not_probability_and_ev_is_independent(self):
        first = assess(
            thesis_id="t-1",
            scenarios=[scenario(label="base", probability=1.0, expected_return=0.05)],
        )
        second = assess(
            thesis_id="t-2",
            scenarios=[scenario(label="base", probability=1.0, expected_return=0.20)],
        )
        self.assertEqual(first.score, second.score)
        self.assertNotEqual(first.expected_value, second.expected_value)
        self.assertNotEqual(first.score, first.expected_value)


class BlockedAndRankingTests(unittest.TestCase):
    def test_blocked_gates_reported_and_ranked_after_eligible(self):
        blocked = assess(thesis_id="t-blocked", liquidity=0.05)
        eligible = assess(thesis_id="t-ok")
        self.assertEqual(blocked.blocked_by, ("liquidity",))
        self.assertEqual(blocked.score, 0.0)
        self.assertFalse(blocked.eligible)
        self.assertTrue(eligible.eligible)
        ranked = rank_opportunities([blocked, eligible])
        self.assertEqual(
            [r.assessment.thesis_id for r in ranked], ["t-ok", "t-blocked"]
        )
        self.assertEqual(ranked[1].position, 2)

    def test_missing_catalyst_blocks_opportunity(self):
        bare = assess(thesis_id="t-bare", catalysts=[])
        self.assertIsNone(bare.catalyst_readiness)
        self.assertEqual(bare.blocked_by, ("catalyst",))
        self.assertEqual(bare.score, 0.0)
        self.assertFalse(bare.eligible)

    def test_rankings_stable_and_input_order_independent(self):
        assessments = [
            assess(
                thesis_id="t-01",
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.20)
                ],
            ),
            assess(
                thesis_id="t-02",
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.15)
                ],
            ),
            assess(
                thesis_id="t-03",
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.10)
                ],
            ),
            assess(
                thesis_id="t-04",
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.05)
                ],
            ),
            assess(
                thesis_id="t-05",
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.01)
                ],
            ),
            assess(
                thesis_id="t-06",
                attention=0.5,
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.12)
                ],
            ),
            assess(
                thesis_id="t-07",
                liquidity=0.05,
                scenarios=[
                    scenario(label="base", probability=1.0, expected_return=0.30)
                ],
            ),
            assess(
                thesis_id="t-08",
                scenarios=[
                    scenario(label="up", probability=0.5, expected_return=0.2),
                    scenario(label="tail", probability=None, expected_return=-0.4),
                ],
            ),
            assess(thesis_id="t-09", catalysts=[]),
        ]
        rng = random.Random(20260815)
        shuffled_a = rng.sample(assessments, len(assessments))
        shuffled_b = rng.sample(assessments, len(assessments))
        ranked_a = rank_opportunities(assessments)
        ranked_b = rank_opportunities(shuffled_a)
        ranked_c = rank_opportunities(shuffled_b)
        expected_order = [
            "t-01",
            "t-02",
            "t-06",
            "t-03",
            "t-04",
            "t-05",
            "t-07",
            "t-08",
            "t-09",
        ]
        self.assertEqual([r.assessment.thesis_id for r in ranked_a], expected_order)
        self.assertEqual([r.assessment.thesis_id for r in ranked_b], expected_order)
        self.assertEqual([r.assessment.thesis_id for r in ranked_c], expected_order)
        self.assertEqual(
            [r.position for r in ranked_a],
            list(range(1, len(assessments) + 1)),
        )
        # every eligible entry precedes every blocked or unpriced entry
        eligible_positions = [r.position for r in ranked_a if r.assessment.eligible]
        ineligible_positions = [
            r.position for r in ranked_a if not r.assessment.eligible
        ]
        self.assertEqual(
            eligible_positions, list(range(1, len(eligible_positions) + 1))
        )
        self.assertEqual(
            ineligible_positions,
            list(range(len(eligible_positions) + 1, len(assessments) + 1)),
        )
        # rank tuples are a total order
        self.assertEqual(len({a.rank_tuple for a in assessments}), len(assessments))
        # explicit tie breakers: identical EV resolves by score
        same_ev_low_score = assess(
            thesis_id="t-low-score",
            attention=0.8,
            scenarios=[scenario(label="base", probability=1.0, expected_return=0.05)],
        )
        same_ev_high_score = assess(
            thesis_id="t-high-score",
            attention=0.1,
            scenarios=[scenario(label="base", probability=1.0, expected_return=0.05)],
        )
        self.assertAlmostEqual(
            same_ev_low_score.expected_value,
            same_ev_high_score.expected_value,
        )
        self.assertLess(same_ev_low_score.score, same_ev_high_score.score)
        tied = rank_opportunities([same_ev_low_score, same_ev_high_score])
        self.assertEqual(
            [r.assessment.thesis_id for r in tied],
            ["t-high-score", "t-low-score"],
        )

    def test_ranking_scales_to_hundreds(self):
        assessments = []
        for index in range(320):
            thesis_id = f"thesis-{index:04d}"
            pattern = index % 4
            if pattern == 0:
                assessments.append(
                    assess(
                        thesis_id=thesis_id,
                        scenarios=[
                            scenario(
                                label="base",
                                probability=1.0,
                                expected_return=0.01 + 0.01 * (index % 80) / 80,
                            )
                        ],
                    )
                )
            elif pattern == 1:
                assessments.append(assess(thesis_id=thesis_id, liquidity=0.05))
            elif pattern == 2:
                assessments.append(
                    assess(
                        thesis_id=thesis_id,
                        scenarios=[
                            scenario(label="up", probability=0.5, expected_return=0.2),
                            scenario(
                                label="tail",
                                probability=None,
                                expected_return=-0.4,
                            ),
                        ],
                    )
                )
            else:
                assessments.append(assess(thesis_id=thesis_id, attention=0.3))
        rng = random.Random(7)
        shuffled = rng.sample(assessments, len(assessments))
        first = rank_opportunities(assessments)
        second = rank_opportunities(shuffled)
        self.assertEqual(
            [r.assessment.thesis_id for r in second],
            [r.assessment.thesis_id for r in first],
        )
        self.assertEqual(first[0].position, 1)
        self.assertEqual(first[-1].position, len(assessments))
        eligible = [r for r in first if r.assessment.eligible]
        ineligible = [r for r in first if not r.assessment.eligible]
        self.assertTrue(eligible)
        self.assertTrue(ineligible)
        self.assertEqual(eligible[-1].position, len(eligible))
        self.assertEqual(ineligible[0].position, len(eligible) + 1)
        # within the eligible tier, expected value descends monotonically
        evs = [r.assessment.expected_value for r in eligible]
        self.assertEqual(evs, sorted(evs, reverse=True))


class AscendingSentinelTests(unittest.TestCase):
    """Missing components sort after known ones, never before.

    Descending rank components are encoded negated, so an unknown (None)
    component must carry the positive infinity sentinel: otherwise it sorts
    ahead of every known value and gains priority it never earned.
    """

    def test_known_confidence_outranks_missing_among_tied_blocked(self):
        known = assess(thesis_id="z-known", liquidity=0.05)
        missing = assess(thesis_id="a-missing", liquidity=0.05, evidence=[])
        self.assertIsNotNone(known.confidence)
        self.assertIsNone(missing.confidence)
        self.assertAlmostEqual(known.expected_value, missing.expected_value)
        self.assertEqual(known.score, missing.score)
        self.assertFalse(known.eligible)
        self.assertFalse(missing.eligible)
        ranked = rank_opportunities([missing, known])
        # the known score ranks first even though its thesis id sorts after
        # the unknown one: the confidence slot dominates the id tie-break
        self.assertEqual(
            [r.assessment.thesis_id for r in ranked],
            ["z-known", "a-missing"],
        )
        self.assertEqual(known.rank_tuple[3], -known.confidence)
        self.assertEqual(missing.rank_tuple[3], float("inf"))

    def test_missing_catalyst_and_neglect_sort_after_known_in_eligible_tier(self):
        # Eligible-tier entries with None components are unreachable through
        # assess_opportunity (every gate must pass), so construct the public
        # OpportunityAssessment directly to exercise the same sentinel
        # pattern for catalyst readiness and neglect.
        def raw_assessment(**overrides):
            base = {
                "thesis_id": "t",
                "as_of": NOW,
                "expected_value": 0.05,
                "expected_shortfall": 0.0,
                "probability_sum": 1.0,
                "missing_probability_mass": 0.0,
                "missing_probability_count": 0,
                "missing_probability_labels": (),
                "transaction_cost": 0.0,
                "evidence_strength": 0.8,
                "confidence": 0.7,
                "catalyst_readiness": 0.9,
                "neglect": 0.4,
                "liquidity": 0.8,
                "downside": 0.2,
                "independent_group_count": 1,
                "blocked_by": (),
                "score": 0.5,
            }
            base.update(overrides)
            return OpportunityAssessment(**base)

        known = raw_assessment(thesis_id="z-known")
        missing_neglect = raw_assessment(thesis_id="b-missing-neglect", neglect=None)
        missing_catalyst = raw_assessment(
            thesis_id="a-missing-catalyst", catalyst_readiness=None
        )
        missing_confidence = raw_assessment(
            thesis_id="c-missing-confidence", confidence=None
        )
        for assessment in (
            known,
            missing_neglect,
            missing_catalyst,
            missing_confidence,
        ):
            self.assertTrue(assessment.eligible)
        ranked = rank_opportunities(
            [missing_confidence, missing_catalyst, missing_neglect, known]
        )
        self.assertEqual(
            [r.assessment.thesis_id for r in ranked],
            [
                "z-known",
                "b-missing-neglect",
                "a-missing-catalyst",
                "c-missing-confidence",
            ],
        )
        self.assertEqual(known.rank_tuple[3], -0.7)
        self.assertEqual(known.rank_tuple[4], -0.9)
        self.assertEqual(known.rank_tuple[5], -0.4)
        self.assertEqual(missing_neglect.rank_tuple[5], float("inf"))
        self.assertEqual(missing_catalyst.rank_tuple[4], float("inf"))
        self.assertEqual(missing_confidence.rank_tuple[3], float("inf"))


class CorrelationPassthroughTests(unittest.TestCase):
    def test_decay_passes_through_to_evidence_scoring(self):
        correlated = [
            signal(
                evidence_id="ev-a",
                independence_key="k1",
                content={"claim": "demand is rising"},
            ),
            signal(
                evidence_id="ev-b",
                independence_key="k1",
                content={"claim": "supply is tightening"},
            ),
        ]
        low_decay = assess(thesis_id="t-low", evidence=correlated, decay=0.1)
        high_decay = assess(thesis_id="t-high", evidence=correlated, decay=0.9)
        self.assertGreater(high_decay.evidence_strength, low_decay.evidence_strength)
        self.assertEqual(high_decay.independent_group_count, 1)
        self.assertEqual(high_decay.expected_value, low_decay.expected_value)


if __name__ == "__main__":
    unittest.main()
