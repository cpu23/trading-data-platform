"""Behavioral tests for bounded, truthful system topology assembly."""

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from topology import build_system_topology  # noqa: E402

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


def _row_for(sql: str):
    if "FROM research_questions" in sql:
        return {
            "question_backlog": 4,
            "question_last_activity": NOW,
            "active_work_orders": 1,
            "work_order_last_activity": NOW,
            "recent_effects": 3,
            "effect_last_activity": NOW,
            "active_skills": 5,
            "available_source_capabilities": 4,
            "capability_last_activity": NOW,
        }
    if "FROM analysis_jobs" in sql:
        return {
            "active_jobs": 2,
            "job_last_activity": NOW,
            "active_planner_jobs": 1,
            "planner_last_activity": NOW,
            "running_collections": 1,
            "collection_last_activity": NOW,
            "scheduler_live": 1,
            "scheduler_last_activity": NOW,
            "worker_live": 1,
            "worker_last_activity": NOW,
            "quote_live": 1,
            "quote_last_activity": NOW,
        }
    if "events_24h" in sql:
        return {
            "events_24h": 10,
            "event_last_activity": NOW,
            "pending_outbox": 1,
            "outbox_last_activity": NOW,
            "recent_ui_invalidations": 2,
            "ui_invalidation_last_activity": NOW,
        }
    raise AssertionError(f"unexpected topology query: {sql}")


class SystemTopologyApiTests(unittest.TestCase):
    @patch("topology.app_config.load_config", return_value={})
    @patch("topology.query_one", side_effect=lambda sql, **_kwargs: _row_for(sql))
    def test_full_topology_is_bounded_linked_and_uses_persisted_activity(
        self, query_one, _config
    ):
        topology = build_system_topology()
        self.assertEqual(topology.status, "available")
        self.assertLessEqual(len(topology.nodes), 64)
        self.assertLessEqual(len(topology.edges), 128)
        by_id = {node.id: node for node in topology.nodes}
        self.assertEqual(by_id["research-planner"].status, "active")
        self.assertEqual(by_id["research-skills"].bounded_count, 5)
        self.assertEqual(by_id["postgresql"].status, "healthy")
        self.assertEqual(query_one.call_count, 3)
        self.assertTrue(
            all(edge.source in by_id and edge.target in by_id for edge in topology.edges)
        )

    @patch("topology.app_config.load_config", return_value={})
    @patch("topology.query_one")
    def test_one_query_failure_is_partial_without_hiding_other_layers(
        self, query_one, _config
    ):
        def partial(sql, **_kwargs):
            if "FROM research_questions" in sql:
                raise RuntimeError("private database detail")
            return _row_for(sql)

        query_one.side_effect = partial
        topology = build_system_topology()
        by_id = {node.id: node for node in topology.nodes}
        self.assertEqual(topology.status, "partial")
        self.assertEqual(topology.unavailable_components, ["research"])
        self.assertEqual(by_id["research-questions"].status, "unavailable")
        self.assertEqual(by_id["research-planner"].status, "active")
        self.assertEqual(by_id["collectors"].status, "active")
        self.assertEqual(by_id["postgresql"].status, "degraded")
        self.assertNotIn("private", topology.summary)

    @patch("topology.app_config.load_config", return_value={})
    @patch("topology.query_one", side_effect=RuntimeError("database unavailable"))
    def test_total_database_failure_never_claims_storage_is_healthy(
        self, _query_one, _config
    ):
        topology = build_system_topology()
        by_id = {node.id: node for node in topology.nodes}
        self.assertEqual(topology.status, "partial")
        self.assertEqual(by_id["postgresql"].status, "unavailable")
        self.assertEqual(by_id["api"].status, "unknown")


if __name__ == "__main__":
    unittest.main()
