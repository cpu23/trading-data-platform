import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from story_confirmation import (
    backfill_story_confirmations,
    calculate_story_confirmation,
    session_target,
)

CLUSTER_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
HEADLINE = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 6, 13, 0, tzinfo=UTC)


class Result:
    def __init__(self, *, first=None, rows=None):
        self._first = first
        self._rows = rows or []

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._rows


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commit = MagicMock()

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.results:
            raise AssertionError(f"unexpected SQL call: {statement}")
        return self.results.pop(0)


class StoryConfirmationTests(unittest.TestCase):
    def test_session_target_rolls_forward_after_configured_close(self):
        before = datetime(2026, 8, 6, 20, 0, tzinfo=UTC)
        after = datetime(2026, 8, 6, 22, 0, tzinfo=UTC)
        settings = {"session_close": "21:00:00"}
        self.assertEqual(session_target(before, settings).day, 6)
        self.assertEqual(session_target(after, settings).day, 7)

    def test_calculation_is_bounded_auditable_and_direction_agnostic(self):
        context = {
            "cluster_id": CLUSTER_ID,
            "event_id": EVENT_ID,
            "headline_at": HEADLINE,
            "markets": [
                {"canonical_id": "EURUSD", "symbol": "EURUSD"},
                {"canonical_id": "SP500", "symbol": "SP500"},
            ],
        }
        rows = [
            {
                "symbol": "EURUSD",
                "timestamp": datetime(2026, 8, 6, 11, 55, tzinfo=UTC),
                "close": 99.0,
            },
            {"symbol": "EURUSD", "timestamp": HEADLINE, "close": 100.0},
            {"symbol": "SP500", "timestamp": HEADLINE, "close": 200.0},
            {
                "symbol": "EURUSD",
                "timestamp": datetime(2026, 8, 6, 12, 5, tzinfo=UTC),
                "close": 101.0,
            },
            {
                "symbol": "SP500",
                "timestamp": datetime(2026, 8, 6, 12, 5, tzinfo=UTC),
                "close": 198.0,
            },
            {
                "symbol": "EURUSD",
                "timestamp": datetime(2026, 8, 6, 12, 30, tzinfo=UTC),
                "close": 99.0,
            },
            {
                "symbol": "SP500",
                "timestamp": datetime(2026, 8, 6, 12, 30, tzinfo=UTC),
                "close": 198.0,
            },
        ]
        session = Session(
            [Result(first=context), Result(rows=rows), Result(), Result()]
        )
        result = calculate_story_confirmation(
            session,
            CLUSTER_ID,
            EVENT_ID,
            {
                "story_confirmation": {
                    "query_limit": 999999,
                    "material_move_percent": 0.25,
                    "session_close": "21:00:00",
                }
            },
            NOW,
        )
        self.assertEqual(result["updated"], 2)
        self.assertIn("confirmed_by_market", result["flags"])
        self.assertIn("market_moved_before_headline", result["flags"])
        self.assertIn("cross_asset_divergence", result["flags"])
        self.assertIn("initial_move_reversed", result["flags"])
        self.assertIn("insufficient_market_data", result["flags"])
        self.assertIn("LIMIT :row_limit", session.calls[1][0])
        self.assertEqual(session.calls[1][1]["row_limit"], 5000)
        persisted_flags = json.loads(session.calls[2][1]["flags"])
        self.assertEqual(persisted_flags, result["flags"])
        provenance = json.loads(session.calls[2][1]["provenance"])
        self.assertEqual(provenance["source_table"], "market_data")
        self.assertNotIn("direction", provenance)
        session.commit.assert_not_called()

    def test_backfill_query_and_limit_are_bounded(self):
        session = Session([Result(rows=[])])
        result = backfill_story_confirmations(session, {}, NOW, limit=100000)
        self.assertEqual(result, {"scanned": 0, "updated": 0})
        self.assertEqual(session.calls[0][1]["limit"], 500)
        self.assertIn("insufficient_market_data", session.calls[0][0])
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
