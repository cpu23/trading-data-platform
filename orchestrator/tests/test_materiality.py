import math
import sys
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from materiality import (
    MaterialityValidationError,
    assess_event_materiality,
    normalize_component,
    score_materiality,
)


class Session:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params or {}))


def event(**overrides):
    value = {
        "event_id": uuid4(),
        "event_type": "macro_release",
        "source": "fred",
        "source_event_id": "fred-1",
        "content_hash": "a" * 64,
        "entities": [{"canonical_id": "EURUSD"}],
        "markets": [],
        "metadata": {},
        "importance_hint": 0.8,
    }
    value.update(overrides)
    return value


class MaterialityMathTests(unittest.TestCase):
    def test_product_score_is_multiplicative(self):
        self.assertEqual(score_materiality(0.8, 0.5, 1.0, 0.9, 0.25), 0.09)

    def test_threshold_boundary_routes(self):
        session = Session()
        result = assess_event_materiality(
            session,
            event(importance_hint=1.0, metadata={"near_duplicate_count": 0}),
            {
                "watched_entities": ["EURUSD"],
                "source_reliability": {"fred": 1.0},
                "analysis_routing": {"event_atom_min_score": 0.5},
            },
        )
        self.assertTrue(result.should_route)
        self.assertEqual(result.score, result.threshold)

    def test_component_normalization_rejects_bounds_and_nonfinite(self):
        with self.assertRaises(MaterialityValidationError):
            normalize_component(-0.01, "importance")
        with self.assertRaises(MaterialityValidationError):
            normalize_component(math.nan, "importance")
        with self.assertRaises(MaterialityValidationError):
            score_materiality(True, 1.0, 1.0, 1.0, 1.0)


class MaterialityAssessmentTests(unittest.TestCase):
    def test_relevance_and_source_defaults_are_auditable(self):
        session = Session()
        result = assess_event_materiality(session, event(entities=[]), {})
        self.assertEqual(result.relevance, 0.5)
        self.assertEqual(result.source_confidence, 0.5)
        self.assertIn("reason", result.rationale["relevance"])
        self.assertEqual(result.provenance["source"], "fred")

    def test_suppression_reason_and_duplicate_novelty(self):
        session = Session()
        result = assess_event_materiality(
            session,
            event(metadata={"exact_duplicate": True}),
            {
                "watched_entities": ["EURUSD"],
                "source_reliability": {"fred": 1.0},
                "analysis_routing": {"event_atom_min_score": 0.01},
            },
        )
        self.assertFalse(result.should_route)
        self.assertEqual(result.suppression_reason, "below_threshold")
        self.assertEqual(result.novelty, 0.0)
        self.assertEqual(result.rationale["novelty"]["reason"], "exact duplicate")

    def test_components_are_stored_as_json_parameters(self):
        session = Session()
        result = assess_event_materiality(session, event(), {})
        self.assertEqual(len(session.calls), 1)
        params = session.calls[0][1]
        self.assertEqual(params["event_id"], result.event_id)
        self.assertIn('"importance"', params["component_rationale"])
        self.assertIn('"source"', params["component_provenance"])

    def test_repeated_call_is_idempotent_upsert(self):
        session = Session()
        source_event = event()
        first = assess_event_materiality(session, source_event, {})
        second = assess_event_materiality(session, source_event, {})
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.score, second.score)
        self.assertEqual(len(session.calls), 2)
        self.assertIn("ON CONFLICT", str(session.calls[0][0]))
        self.assertIn("event_id, job_type", str(session.calls[0][0]))

    def test_macro_series_mapping_priority_and_instruments_drive_components(self):
        session = Session()
        result = assess_event_materiality(
            session,
            event(
                importance_hint=None,
                payload={"series_id": "CPIAUCSL"},
                entities=[],
            ),
            {
                "macro_event_mappings": {
                    "CPIAUCSL": {"priority": 10, "instruments": ["SPY"]}
                },
                "source_reliability": {"fred": 1.0},
                "watchlist": {"trading": [{"symbol": "SPY", "type": "equity"}]},
            },
        )
        self.assertEqual(result.importance, 1.0)
        self.assertEqual(result.relevance, 1.0)
        self.assertEqual(result.rationale["relevance"]["matched_entities"], ["SPY"])


if __name__ == "__main__":
    unittest.main()
