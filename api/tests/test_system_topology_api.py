"""Behavioral tests for the three-service system topology."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from topology import build_system_topology, unavailable_system_topology


class SystemTopologyApiTests(unittest.TestCase):
    def test_topology_contains_exact_runtime_services(self):
        now = datetime.now(UTC)

        def rows(sql, **_kwargs):
            if "FROM jobs" in sql:
                return {"active_jobs": 4, "last_job_at": now}
            if "FROM role_heartbeats" in sql:
                return {"status": "running", "last_heartbeat_at": now}
            raise AssertionError(sql)

        with patch("topology.query_one", side_effect=rows):
            topology = build_system_topology()

        self.assertEqual(topology.status, "available")
        self.assertEqual(
            {node.id for node in topology.nodes}, {"postgres", "web", "worker"}
        )
        self.assertEqual(
            {(edge.source, edge.target) for edge in topology.edges},
            {("web", "postgres"), ("worker", "postgres")},
        )
        worker = next(node for node in topology.nodes if node.id == "worker")
        self.assertEqual(worker.status, "healthy")
        self.assertEqual(worker.bounded_count, 4)

    def test_stale_worker_heartbeat_is_truthful(self):
        now = datetime.now(UTC)

        def rows(sql, **_kwargs):
            if "FROM jobs" in sql:
                return {"active_jobs": 0, "last_job_at": None}
            return {
                "status": "running",
                "last_heartbeat_at": now - timedelta(minutes=2),
            }

        with patch("topology.query_one", side_effect=rows):
            topology = build_system_topology()

        worker = next(node for node in topology.nodes if node.id == "worker")
        self.assertEqual(worker.status, "stale")
        self.assertEqual(worker.staleness_reason, "worker heartbeat is stale")

    def test_database_failure_is_partial_and_redacted(self):
        with patch("topology.query_one", side_effect=RuntimeError("private")):
            topology = build_system_topology()

        self.assertEqual(topology.status, "partial")
        self.assertEqual(topology.unavailable_components, ["postgres", "worker"])
        postgres = next(node for node in topology.nodes if node.id == "postgres")
        self.assertEqual(postgres.status, "unavailable")
        self.assertNotIn("private", topology.model_dump_json())

    def test_unavailable_fallback_has_no_invented_nodes(self):
        topology = unavailable_system_topology()
        self.assertEqual(topology.status, "unavailable")
        self.assertEqual(topology.nodes, [])
        self.assertEqual(topology.edges, [])


if __name__ == "__main__":
    unittest.main()
