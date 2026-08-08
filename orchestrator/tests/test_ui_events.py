import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from section_snapshots import publish_section_snapshot  # noqa: E402
from ui_events import (  # noqa: E402
    append_ui_event,
    delete_expired_ui_events,
    get_ui_event_bounds,
    list_ui_event_replay,
    parse_ui_event_row,
)


class _Result:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _Session:
    def __init__(self):
        self.snapshots = []
        self.events = []
        self.commits = 0
        self.bind = type(
            "Bind", (), {"dialect": type("Dialect", (), {"name": "sqlite"})()}
        )()

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if sql.startswith("SELECT * FROM section_snapshots"):
            rows = [
                row
                for row in self.snapshots
                if row["section_key"] == params["section_key"]
                and row["scope_key"] == params["scope_key"]
                and row["status"] == "published"
            ]
            rows.sort(key=lambda row: row["version"], reverse=True)
            return _Result(rows[: params.get("limit", 1)])
        if "COALESCE(MAX(version)" in sql:
            versions = [
                row["version"]
                for row in self.snapshots
                if row["section_key"] == params["section_key"]
                and row["scope_key"] == params["scope_key"]
            ]
            return _Result([{"max_version": max(versions, default=0)}])
        if sql.startswith("UPDATE section_snapshots"):
            for row in self.snapshots:
                if row["id"] == params["snapshot_id"]:
                    row["status"] = "superseded"
            return _Result()
        if sql.startswith("INSERT INTO section_snapshots"):
            row = {
                "id": len(self.snapshots) + 1,
                "section_key": params["section_key"],
                "scope_key": params["scope_key"],
                "version": params["version"],
                "status": "published",
                "content_hash": params["content_hash"],
            }
            self.snapshots.append(row)
            return _Result([row])
        if sql.startswith("INSERT INTO ui_events"):
            row = {
                "id": len(self.events) + 1,
                "event_name": params["event_name"],
                "section_key": params["section_key"],
                "scope_key": params["scope_key"],
                "section_version": params["section_version"],
                "payload": json.loads(params["payload"]),
                "created_at": datetime.now(UTC),
                "expires_at": datetime.now(UTC) + timedelta(hours=48),
            }
            self.events.append(row)
            return _Result([row])
        if sql.startswith("SELECT id, event_name"):
            rows = [row for row in self.events if row["id"] > params["after_id"]]
            return _Result(rows[: params["limit"]])
        if sql.startswith("SELECT MIN(id)"):
            ids = [row["id"] for row in self.events]
            return _Result(
                [{"min_id": min(ids, default=None), "max_id": max(ids, default=None)}]
            )
        if sql.startswith("DELETE FROM ui_events"):
            expired = self.events[: params["limit"]]
            self.events = self.events[len(expired) :]
            return _Result(rowcount=len(expired))
        return _Result()


class UiEventTests(unittest.TestCase):
    def test_changed_snapshot_emits_and_noop_does_not(self):
        session = _Session()
        first = publish_section_snapshot(
            session, section_key="watchlist", payload={"v": 1}
        )
        second = publish_section_snapshot(
            session, section_key="watchlist", payload={"v": 1}
        )
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(len(session.events), 1)
        self.assertEqual(session.events[0]["event_name"], "watchlist_changed")
        self.assertEqual(session.events[0]["section_version"], 1)

    def test_payload_and_names_are_allowlisted(self):
        session = _Session()
        with self.assertRaises(ValueError):
            append_ui_event(
                session,
                event_name="section_changed",
                section_key="watchlist",
                section_version=1,
                payload={"credential": "secret"},
            )
        self.assertIsNone(
            parse_ui_event_row({"event_name": "private", "section_key": "x"})
        )

    def test_replay_is_bounded_and_bounds_are_retained(self):
        session = _Session()
        for version in range(1, 104):
            append_ui_event(
                session,
                event_name="section_changed",
                section_key="health",
                section_version=version,
            )
        rows = list_ui_event_replay(session, limit=1000)
        self.assertEqual(len(rows), 100)
        self.assertEqual(get_ui_event_bounds(session), (1, 103))

    def test_cleanup_is_bounded(self):
        session = _Session()
        for version in range(1, 104):
            append_ui_event(
                session,
                event_name="section_changed",
                section_key="health",
                section_version=version,
            )
        self.assertEqual(delete_expired_ui_events(session, limit=1000), 100)
        self.assertEqual(len(session.events), 3)
        self.assertEqual(session.commits, 0)


if __name__ == "__main__":
    unittest.main()
