from __future__ import annotations

import itertools
import os
import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DEPLOYMENT_MODE", "test")

from research_control_plane.domain import question_fingerprint, question_key
from research_control_plane.repository import (
    questions_from_event,
    questions_from_falsification,
    questions_from_promoted_candidate,
)

CUTOFF = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
THESIS_ID = UUID("00000000-0000-0000-0000-000000000101")


class QuestionGenerationTests(unittest.TestCase):
    def test_promoted_missing_evidence_is_atomic_bounded_and_cutoff_stable(
        self,
    ) -> None:
        candidate = {
            "missing_evidence": [
                "Current issuer revenue guidance",
                "Peer supplier read-through?",
                "Options positioning divergence",
            ],
            "confidence": 0.4,
            "opportunity_score": 0.8,
        }

        questions = questions_from_promoted_candidate(
            candidate, thesis_id=THESIS_ID, accepted_cutoff=CUTOFF
        )

        self.assertEqual(
            [item.candidate.question_type for item in questions],
            [
                "earnings_guidance_delta",
                "filing_peer_readthrough",
                "positioning_divergence",
            ],
        )
        self.assertTrue(
            all(item.candidate.atomic_question.endswith("?") for item in questions)
        )
        self.assertEqual(questions[0].priority.materiality, Decimal("0.8"))
        self.assertEqual(questions[0].priority.uncertainty, Decimal("0.6"))
        first_fingerprint = question_fingerprint(questions[0].candidate)
        replay_fingerprint = question_fingerprint(
            questions_from_promoted_candidate(
                candidate, thesis_id=THESIS_ID, accepted_cutoff=CUTOFF
            )[0].candidate
        )
        self.assertEqual(first_fingerprint, replay_fingerprint)
        changed_cutoff = questions_from_promoted_candidate(
            candidate,
            thesis_id=THESIS_ID,
            accepted_cutoff=datetime(2026, 8, 23, 12, 1, tzinfo=UTC),
        )[0]
        self.assertNotEqual(
            first_fingerprint, question_fingerprint(changed_cutoff.candidate)
        )
        self.assertEqual(
            question_key(questions[0].candidate),
            question_key(changed_cutoff.candidate),
        )

    def test_unknown_candidate_metrics_remain_unknown_not_zero(self) -> None:
        question = questions_from_promoted_candidate(
            {"missing_evidence": ["Unreported customer concentration"]},
            thesis_id=THESIS_ID,
            accepted_cutoff=CUTOFF,
        )[0]

        self.assertIsNone(question.priority.materiality)
        self.assertIsNone(question.priority.uncertainty)
        self.assertEqual(question.priority.expected_cost_usd, Decimal("0.05"))

    def test_falsification_required_data_accepts_typed_and_text_entries(self) -> None:
        questions = questions_from_falsification(
            {
                "required_data": [
                    {"description": "Independent churn cohort evidence"},
                    "Current customer renewal disclosure",
                    {},
                    "  ",
                ]
            },
            thesis_id=THESIS_ID,
            accepted_cutoff=CUTOFF,
            materiality="0.7",
            uncertainty="0.9",
        )

        self.assertEqual(len(questions), 2)
        self.assertTrue(
            all(item.candidate.origin_kind == "falsification" for item in questions)
        )
        self.assertTrue(
            all(
                item.candidate.question_type == "thesis_challenge" for item in questions
            )
        )

    def test_event_questions_are_entity_targeted_permutation_invariant_and_bounded(
        self,
    ) -> None:
        entities = [
            {"name": "Beta Corp"},
            {"canonical_id": "issuer:alpha"},
            {"name": "Beta Corp"},
        ]
        fingerprints: set[tuple[str, ...]] = set()
        for permutation in itertools.permutations(entities):
            questions = questions_from_event(
                {
                    "event_id": "00000000-0000-0000-0000-000000000999",
                    "event_type": "filing_ingested",
                    "source": "sec",
                    "entities": list(permutation),
                    "importance_hint": 0.75,
                },
                accepted_cutoff=CUTOFF,
            )
            fingerprints.add(
                tuple(question_fingerprint(item.candidate) for item in questions)
            )

        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(len(next(iter(fingerprints))), 2)
        self.assertTrue(
            all(
                item.candidate.acceptable_source_families == ("sec",)
                for item in questions
            )
        )
        self.assertEqual(questions[0].priority.materiality, Decimal("0.75"))

    def test_source_event_without_targets_is_a_successful_noop(self) -> None:
        self.assertEqual(
            questions_from_event(
                {"event_type": "source_freshness_changed", "entities": []},
                accepted_cutoff=CUTOFF,
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
