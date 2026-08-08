import math
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from macro_releases import (
    advance_stage,
    build_macro_release_card,
    canonical_release_identity,
    list_macro_release_cards,
    standardized_surprise,
    upsert_macro_release_card,
)


class _Result:
    def __init__(self, rows=(), first=None):
        self.rows = list(rows)
        self._first = first

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return (
            self._first
            if self._first is not None
            else (self.rows[0] if self.rows else None)
        )


class _Session:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return _Result(rows=self.rows)


class _HistorySession:
    def __init__(self, history):
        self.history = history
        self.calls = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "SELECT actual, consensus, absolute_surprise" in sql:
            return _Result(rows=self.history)
        return _Result()


def event(**payload):
    return SimpleNamespace(
        event_id=uuid4(),
        event_type="macro_release",
        source="fred",
        source_event_id="GDP:2026-08-05T12:00:00+00:00",
        revision_of_event_id=None,
        observed_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        importance_hint=0.8,
        payload={
            "series_id": "GDP",
            "event_name": "US GDP",
            "observed_at": "2026-08-05T12:00:00Z",
            **payload,
        },
        metadata={"release_id": "GDP:2026-08-05", "units": "percent"},
    )


class MacroReleaseCardTests(unittest.TestCase):
    def test_t0_card_construction_is_deterministic_and_keeps_provenance(self):
        source = event(
            actual=3.2, consensus=3.0, previous=2.9, released_at="2026-08-05T12:00:00Z"
        )
        first = build_macro_release_card(source)
        second = build_macro_release_card(source)
        self.assertEqual(first, second)
        self.assertEqual(first["stage"], "t0")
        self.assertEqual(first["absolute_surprise"], 0.2)
        self.assertEqual(first["impact"], "high")
        self.assertEqual(
            first["source_event_provenance"]["payload"]["series_id"], "GDP"
        )
        self.assertNotIn("llm", sys.modules)

    def test_config_mapping_names_unlabeled_series_and_sets_impact(self):
        source = event(series_id="PAYEMS", actual=180.0, consensus=160.0)
        source.payload.pop("event_name")
        card = build_macro_release_card(
            source,
            config={
                "macro_event_mappings": {
                    "PAYEMS": {"event_name": "US nonfarm payrolls", "priority": 10}
                }
            },
        )
        self.assertEqual(card["event_name"], "US nonfarm payrolls")
        self.assertEqual(card["series_id"], "PAYEMS")
        self.assertEqual(card["impact"], "high")

    def test_upsert_uses_prior_series_history_for_standardized_surprise(self):
        source = event(series_id="PAYEMS", actual=4.0, consensus=0.0, previous=1.0)
        session = _HistorySession(
            [
                {"actual": 1.0, "consensus": 0.0, "absolute_surprise": 1.0},
                {"actual": 2.0, "consensus": 0.0, "absolute_surprise": 2.0},
            ]
        )
        card = upsert_macro_release_card(session, source)
        self.assertAlmostEqual(card["standardized_surprise"], 5.0)
        self.assertNotIn("insufficient_history", card["quality_flags"])
        history_call = next(
            sql for sql, _ in session.calls if "SELECT actual, consensus" in sql
        )
        self.assertIn("series_id = :series_id", history_call)

    def test_missing_consensus_has_explicit_quality_and_developing_fields(self):
        card = build_macro_release_card(event(actual=3.2, previous=2.9))
        self.assertIsNone(card["absolute_surprise"])
        self.assertIn("missing_consensus", card["quality_flags"])
        self.assertFalse(card["developing"])
        self.assertEqual(card["developing_fields"], ["consensus"])

    def test_pre_release_without_actual_is_explicitly_developing(self):
        card = build_macro_release_card(event(consensus=3.0, previous=2.9))
        self.assertEqual(card["stage"], "developing")
        self.assertTrue(card["developing"])
        self.assertIn("actual", card["developing_fields"])

    def test_standardized_surprise_requires_history_and_is_finite(self):
        self.assertIsNone(standardized_surprise(2.0, [1.0]))
        score = standardized_surprise(2.0, [0.0, 1.0, 2.0])
        self.assertAlmostEqual(score, 1.2247448714)
        self.assertIsNone(standardized_surprise(float("nan"), [0.0, 1.0]))
        self.assertTrue(math.isfinite(score))

    def test_publisher_payload_metadata_supplies_release_values(self):
        source = event(
            value=3.2,
            metadata={"consensus": 3.0, "previous": 2.9},
            released_at="2026-08-05T12:00:00Z",
        )
        card = build_macro_release_card(source)
        self.assertEqual(card["actual"], 3.2)
        self.assertEqual(card["consensus"], 3.0)
        self.assertEqual(card["previous"], 2.9)
        self.assertEqual(card["absolute_surprise"], 0.2)

    def test_revision_keeps_identity_and_links_revision_provenance(self):
        original = event(actual=3.0, consensus=3.0)
        revision = event(actual=3.2, consensus=3.0, revised_previous=2.9)
        revision.event_type = "macro_revision"
        revision.revision_of_event_id = original.event_id
        self.assertEqual(
            canonical_release_identity(original), canonical_release_identity(revision)
        )
        card = build_macro_release_card(revision)
        self.assertIn("revision", card["quality_flags"])
        self.assertEqual(
            card["source_event_provenance"]["revision_of_event_id"],
            str(original.event_id),
        )
        self.assertEqual(card["revised_previous"], 2.9)

    def test_stage_advancement_is_monotonic(self):
        self.assertEqual(advance_stage("reaction", "t0"), "reaction")
        self.assertEqual(advance_stage("developing", actual_present=True), "t0")
        self.assertEqual(advance_stage("t0", reaction_available=True), "reaction")
        self.assertEqual(advance_stage("reaction", finalized=True), "final")

    def test_bounded_reads_clamp_limit_and_keep_current_lookup_shape(self):
        session = _Session()
        list_macro_release_cards(session, limit=100000, offset=-4, current_only=True)
        sql, params = session.calls[-1]
        self.assertIn("macro_release_cards_current", sql)
        self.assertEqual(params["limit"], 500)
        self.assertEqual(params["offset"], 0)


if __name__ == "__main__":
    unittest.main()
