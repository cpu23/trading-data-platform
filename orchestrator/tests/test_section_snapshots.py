import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events.canonicalize import content_hash  # noqa: E402
from section_snapshots import (  # noqa: E402
    SnapshotValidationError,
    get_current_snapshot,
    list_snapshot_history,
    publish_section_snapshot,
)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _FakeSession:
    """Small SQLite-like fake proving transaction ownership and SQL intent."""

    def __init__(self):
        self.rows = []
        self.next_id = 1
        self.commits = 0
        self.mutations = []
        self.bind = type(
            "Bind", (), {"dialect": type("Dialect", (), {"name": "sqlite"})()}
        )()

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if sql.startswith("SELECT * FROM section_snapshots"):
            rows = [
                row
                for row in self.rows
                if row["section_key"] == params.get("section_key")
                and row["scope_key"] == params.get("scope_key")
                and ("status = 'published'" not in sql or row["status"] == "published")
            ]
            rows.sort(key=lambda row: row["version"], reverse=True)
            if "before_version" in params:
                rows = [
                    row for row in rows if row["version"] < params["before_version"]
                ]
            return _Result(rows[: params.get("limit", 1)])
        if "COALESCE(MAX(version)" in sql:
            values = [
                row["version"]
                for row in self.rows
                if row["section_key"] == params["section_key"]
                and row["scope_key"] == params["scope_key"]
            ]
            return _Result([{"max_version": max(values, default=0)}])
        if sql.startswith("UPDATE section_snapshots"):
            self.mutations.append("update")
            for row in self.rows:
                if row["id"] == params["snapshot_id"] and row["status"] == "published":
                    row["status"] = "superseded"
            return _Result()
        if sql.startswith("INSERT INTO section_snapshots"):
            self.mutations.append("insert")
            row = {
                "id": self.next_id,
                "section_key": params["section_key"],
                "scope_key": params["scope_key"],
                "version": params["version"],
                "status": "published",
                "payload": params["payload"],
                "content_hash": params["content_hash"],
                "source_event_ids": params["source_event_ids"],
                "published_at": datetime.now(UTC),
            }
            self.next_id += 1
            self.rows.append(row)
            return _Result([row])
        return _Result()


class SectionSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.session = _FakeSession()

    def test_unchanged_payload_is_a_noop(self):
        first = publish_section_snapshot(
            self.session, section_key="health", payload={"ok": True}
        )
        self.session.mutations.clear()
        second = publish_section_snapshot(
            self.session, section_key="health", payload={"ok": True}
        )
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(second.version, 1)
        self.assertEqual(self.session.mutations, [])

    def test_validation_failure_preserves_current(self):
        publish_section_snapshot(
            self.session, section_key="health", payload={"version": 1}
        )
        self.session.mutations.clear()
        with self.assertRaises(SnapshotValidationError):
            publish_section_snapshot(
                self.session,
                section_key="health",
                payload={"version": 2},
                validator=lambda payload: False,
            )
        self.assertEqual(self.session.mutations, [])
        current = get_current_snapshot(self.session, section_key="health")
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["status"], "published")

    def test_next_version_supersedes_previous(self):
        first = publish_section_snapshot(
            self.session, section_key="health", payload={"version": 1}
        )
        second = publish_section_snapshot(
            self.session, section_key="health", payload={"version": 2}
        )
        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(self.session.rows[0]["status"], "superseded")
        self.assertEqual(self.session.rows[1]["status"], "published")

    def test_hash_is_deterministic_and_source_ids_are_bounded(self):
        event_ids = [uuid4() for _ in range(300)]
        left = publish_section_snapshot(
            self.session,
            section_key="health",
            payload={"b": 2, "a": 1},
            source_event_ids=event_ids,
        )
        self.assertEqual(left.content_hash, content_hash({"a": 1, "b": 2}))
        self.assertEqual(len(self.session.rows[0]["source_event_ids"]), 256)
        self.assertEqual(self.session.rows[0]["source_event_ids"], event_ids[-256:])

    def test_history_is_bounded(self):
        for version in range(3):
            publish_section_snapshot(
                self.session, section_key="health", payload={"version": version}
            )
        history = list_snapshot_history(self.session, section_key="health", limit=1)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["version"], 3)

    def test_payload_must_be_object_or_array(self):
        with self.assertRaises(TypeError):
            publish_section_snapshot(
                self.session, section_key="health", payload="private"
            )


if __name__ == "__main__":
    unittest.main()
