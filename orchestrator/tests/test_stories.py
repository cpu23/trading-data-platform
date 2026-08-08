import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stories import cluster_news_story, normalize_title, token_overlap

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
CLUSTER_ID = UUID("11111111-1111-4111-8111-111111111111")
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")


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


def event(*, source="reuters", item_id="r-1", title="Fed holds rates", tags=None):
    return SimpleNamespace(
        event_id=EVENT_ID,
        source=source,
        source_event_id=item_id,
        observed_at=NOW,
        effective_at=NOW,
        published_at=NOW,
        importance_hint=0.8,
        entities=[],
        markets=[{"canonical_id": "EURUSD", "symbol": "EURUSD"}],
        payload={
            "id": item_id,
            "title": title,
            "summary": "A bounded public summary.",
            "published": NOW.isoformat(),
            "tags": tags or [],
            "source_label": source.title(),
            "url": "https://example.com/story",
        },
    )


def cluster_row(**overrides):
    value = {
        "id": CLUSTER_ID,
        "canonical_key": "story:key",
        "title": "Fed holds rates",
        "summary": "Initial summary",
        "state": "developing",
        "lane": "low_confidence",
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "last_material_change_at": NOW,
        "importance": 0.8,
        "novelty": 1.0,
        "confidence": 0.68,
        "entities": [],
        "markets": [{"canonical_id": "EURUSD", "symbol": "EURUSD"}],
        "source_count": 1,
        "version": 1,
        "change_summary": "Initial report",
        "clustering_reason": {},
        "created_at": NOW,
        "updated_at": NOW,
    }
    value.update(overrides)
    return value


CONFIG = {
    "story_clustering": {
        "similarity_threshold": 0.3,
        "material_change_threshold": 0.4,
        "watchlist": ["EURUSD"],
        "source_confidence": {"default": 0.6, "reuters": 0.85, "kobeissi": 0.65},
    }
}


class StoryClusteringTests(unittest.TestCase):
    def test_title_normalization_and_overlap_are_deterministic(self):
        self.assertEqual(normalize_title("  FED—holds RATES!! "), "fed holds rates")
        self.assertEqual(token_overlap("Fed holds rates", "Rates held by Fed"), 0.5)

    def test_single_source_creates_explicit_low_confidence_cluster_without_commit(self):
        created = cluster_row()
        session = Session(
            [
                Result(first=None),
                Result(rows=[]),
                Result(first=created),
                Result(first={"id": 7}),
                Result(),
            ]
        )
        assignment = cluster_news_story(session, event(), CONFIG, NOW)
        self.assertEqual(assignment.cluster_id, CLUSTER_ID)
        self.assertEqual(assignment.lane, "low_confidence")
        self.assertTrue(assignment.materially_changed)
        self.assertEqual(assignment.contribution_type, "origin")
        self.assertIn("LIMIT :candidate_limit", session.calls[1][0])
        self.assertEqual(session.calls[1][1]["candidate_limit"], 100)
        version_params = session.calls[-1][1]
        self.assertIn(NOW.isoformat(), version_params["snapshot"])
        session.commit.assert_not_called()

    def test_exact_source_item_retry_is_a_one_query_noop(self):
        session = Session([Result(first=cluster_row(version=3, state="confirmed"))])
        assignment = cluster_news_story(session, event(), CONFIG, NOW)
        self.assertFalse(assignment.materially_changed)
        self.assertEqual(assignment.version, 3)
        self.assertEqual(len(session.calls), 1)
        session.commit.assert_not_called()

    def test_same_source_repeated_coverage_adds_evidence_without_new_version(self):
        session = Session(
            [
                Result(first=None),
                Result(rows=[cluster_row()]),
                Result(rows=[{"source": "reuters"}]),
                Result(first={"id": 8}),
                Result(),
            ]
        )
        assignment = cluster_news_story(
            session,
            event(item_id="r-2", title="Fed holds rates today"),
            CONFIG,
            NOW,
        )
        self.assertFalse(assignment.materially_changed)
        self.assertEqual(assignment.contribution_type, "repeated_coverage")
        self.assertEqual(len(session.calls), 5)
        self.assertFalse(
            any("story_cluster_versions" in sql for sql, _ in session.calls)
        )

    def test_cross_source_confirmation_changes_state_and_appends_audit_version(self):
        updated = cluster_row(
            version=2,
            state="confirmed",
            lane="watchlist_related",
            source_count=2,
            confidence=0.75,
            change_summary="Confirmed by an additional source",
        )
        session = Session(
            [
                Result(first=None),
                Result(rows=[cluster_row()]),
                Result(rows=[{"source": "reuters"}]),
                Result(first={"id": 9}),
                Result(first=updated),
                Result(),
            ]
        )
        assignment = cluster_news_story(
            session,
            event(source="kobeissi", item_id="k-1"),
            CONFIG,
            NOW,
        )
        self.assertTrue(assignment.materially_changed)
        self.assertEqual(assignment.state, "confirmed")
        self.assertEqual(assignment.version, 2)
        self.assertEqual(assignment.contribution_type, "cross_source_confirmation")
        audit = [
            params for sql, params in session.calls if "story_cluster_versions" in sql
        ]
        self.assertEqual(audit[0]["prior_state"], "developing")
        self.assertEqual(audit[0]["contribution"], "cross_source_confirmation")

    def test_explicit_contradiction_is_a_material_audited_state_change(self):
        updated = cluster_row(
            version=2,
            state="contradicted",
            change_summary="Contradictory evidence added",
        )
        session = Session(
            [
                Result(first=None),
                Result(rows=[cluster_row()]),
                Result(rows=[{"source": "reuters"}]),
                Result(first={"id": 10}),
                Result(first=updated),
                Result(),
            ]
        )
        assignment = cluster_news_story(
            session,
            event(
                item_id="r-3",
                title="Fed holds rates but denies report",
                tags=["contradiction"],
            ),
            CONFIG,
            NOW,
        )
        self.assertTrue(assignment.materially_changed)
        self.assertEqual(assignment.state, "contradicted")
        self.assertEqual(assignment.contribution_type, "contradiction")
        self.assertTrue(
            any("story_cluster_versions" in sql for sql, _ in session.calls)
        )


if __name__ == "__main__":
    unittest.main()
