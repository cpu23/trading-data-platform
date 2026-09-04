import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events.publisher import publish_news_records
from events.routing import _enqueue_story_jobs

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
EVENT_ID = UUID("22222222-2222-4222-8222-222222222222")
CLUSTER_ID = UUID("11111111-1111-4111-8111-111111111111")


class PhaseSixNewsEventTests(unittest.TestCase):
    @patch("events.publisher.insert_event")
    @patch("events.publisher.build_market_event")
    @patch("events.publisher.find_latest_event", return_value=None)
    @patch("events.publisher.upsert_raw", return_value=1)
    def test_news_adapter_writes_allowlisted_cache_and_event_in_caller_session(
        self, upsert, _latest, build, insert
    ):
        session = MagicMock()
        session.commit = MagicMock()
        built_event = SimpleNamespace(event_id=EVENT_ID)
        build.return_value = built_event
        insert.return_value = SimpleNamespace(inserted=True, outbox_inserted=True)
        record = {
            "id": "reuters-1",
            "source": "reuters",
            "source_label": "Reuters",
            "title": "Fed holds rates",
            "summary": "Summary",
            "url": "https://example.com/story",
            "published": NOW.isoformat(),
            "symbols": ["eurusd"],
            "tags": ["rates"],
            "private_provider_field": "must-not-persist",
        }
        result = publish_news_records([record], session=session)
        self.assertEqual(result.raw_written, 1)
        self.assertEqual(result.events_inserted, 1)
        self.assertEqual(result.outbox_inserted, 1)
        self.assertEqual(upsert.call_args.args[1], "source_payload_cache")
        cached = upsert.call_args.args[2]
        self.assertEqual(cached["raw_payload"]["symbols"], ["EURUSD"])
        self.assertNotIn("private_provider_field", cached["raw_payload"])
        self.assertEqual(upsert.call_args.args[3], ["cache_key"])
        event_payload = build.call_args.args[3]
        self.assertNotIn("private_provider_field", event_payload)
        insert.assert_called_once_with(session, built_event, topic="market_event")
        session.commit.assert_not_called()

    def test_news_adapter_rejects_unbounded_batch_before_database_use(self):
        with self.assertRaisesRegex(ValueError, "exceeds 1000"):
            publish_news_records(
                ({"id": str(index)} for index in range(1001)), session=MagicMock()
            )


class StoryJobRoutingTests(unittest.TestCase):
    @patch("jobs.enqueue_job")
    def test_repeated_coverage_republishes_story_snapshot_only(self, enqueue):
        enqueue.side_effect = lambda _session, **kwargs: kwargs
        assignment = SimpleNamespace(
            cluster_id=CLUSTER_ID, version=1, materially_changed=False
        )
        jobs = _enqueue_story_jobs(
            MagicMock(),
            SimpleNamespace(effective_at=NOW, observed_at=NOW, markets=[]),
            assignment,
            {},
            event_hash="a" * 64,
            correlation_id=EVENT_ID,
            source_event_id=EVENT_ID,
        )
        self.assertEqual(
            [job["job_type"] for job in jobs], ["publish_story_clusters_snapshot"]
        )

    @patch("jobs.enqueue_job")
    def test_material_story_schedules_bounded_confirmation_milestones(self, enqueue):
        enqueue.side_effect = lambda _session, **kwargs: kwargs
        assignment = SimpleNamespace(
            cluster_id=CLUSTER_ID, version=2, materially_changed=True
        )
        jobs = _enqueue_story_jobs(
            MagicMock(),
            SimpleNamespace(
                effective_at=NOW,
                observed_at=NOW,
                markets=[{"symbol": "EURUSD"}],
            ),
            assignment,
            {"story_confirmation": {"session_close": "21:00:00"}},
            event_hash="b" * 64,
            correlation_id=EVENT_ID,
            source_event_id=EVENT_ID,
        )
        self.assertEqual(len(jobs), 4)
        self.assertEqual(jobs[0]["job_type"], "publish_story_clusters_snapshot")
        self.assertEqual(
            [job["payload"]["horizon"] for job in jobs[1:]],
            ["t5", "t30", "session"],
        )
        self.assertTrue(all(job["not_before"] >= NOW for job in jobs[1:]))


class StorySnapshotHandlerTests(unittest.TestCase):
    @patch("section_snapshots.publish_section_snapshot")
    @patch("stories.list_story_clusters")
    @patch(
        "analysis_job_handlers._job_settings",
        return_value={"query": {"max_story_clusters": 25}},
    )
    def test_story_snapshot_is_public_bounded_and_includes_confirmations(
        self, _settings, list_clusters, publish
    ):
        from analysis_job_handlers import publish_story_clusters_snapshot

        list_clusters.return_value = [
            {
                "id": CLUSTER_ID,
                "canonical_key": "story:key",
                "title": "Fed holds rates",
                "summary": "Summary",
                "state": "developing",
                "lane": "low_confidence",
                "first_seen_at": NOW,
                "last_seen_at": NOW,
                "last_material_change_at": NOW,
                "importance": 0.8,
                "novelty": 1.0,
                "confidence": 0.5,
                "entities": [],
                "markets": [],
                "source_count": 1,
                "version": 1,
                "change_summary": "Initial report",
                "clustering_reason": {"private": "not-public"},
            }
        ]
        confirmation_rows = [
            {
                "cluster_id": CLUSTER_ID,
                "market_symbol": "EURUSD",
                "observed_at": NOW,
                "pre_headline_move": 0.1,
                "move_5m": 0.4,
                "move_30m": None,
                "move_session": None,
                "flags": ["confirmed_by_market"],
            }
        ]
        session = MagicMock()
        session.execute.return_value = confirmation_rows
        publish.return_value = "snapshot"
        output = publish_story_clusters_snapshot(
            session, {"source_event_id": str(EVENT_ID)}
        )
        self.assertEqual(output, "snapshot")
        list_clusters.assert_called_once_with(session, limit=25)
        payload = publish.call_args.kwargs["payload"]
        self.assertEqual(len(payload["clusters"]), 1)
        self.assertNotIn("clustering_reason", payload["clusters"][0])
        self.assertEqual(
            payload["clusters"][0]["market_confirmations"][0]["market_symbol"],
            "EURUSD",
        )
        self.assertIn("LIMIT :limit", str(session.execute.call_args.args[0]))
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
