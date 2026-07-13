import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, PropertyMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db


# ── helpers ──────────────────────────────────────────────────────────

def _make_mock_session(*, fail_indices: set[int] | None = None):
    """Return a mock session context manager.

    Each call to ``session.execute`` succeeds unless its (0-indexed)
    invocation number is in *fail_indices*.  A successful execute returns
    a mock result; a failing one raises a ``ValueError`` with a distinct
    per-index message.
    """
    call_count = [0]
    session = MagicMock()
    # begin_nested() returns a nested transaction mock
    nested = MagicMock()
    session.begin_nested.return_value = nested

    def _execute(stmt, params):
        idx = call_count[0]
        call_count[0] = idx + 1
        if fail_indices and idx in fail_indices:
            raise ValueError(f"simulated failure at index {idx}")
        result = MagicMock()
        result.rowcount = 1
        return result

    session.execute.side_effect = _execute
    session.__enter__.return_value = session

    # Context manager protocol: __exit__ must be callable
    session.__exit__ = MagicMock(return_value=False)
    return session


def _patch_get_session(session):
    """Patch ``db.get_session`` to return *session* (a context manager)."""
    return patch.object(db, "get_session", return_value=session)


# ── WriteResult dataclass tests ─────────────────────────────────────

class WriteResultDataclassTests(unittest.TestCase):
    """Test the structure / invariants of the WriteResult dataclass itself."""

    def test_dataclass_exists_and_is_frozen(self):
        result = db.WriteResult(attempted=5, written=5, failed=0, errors=())
        self.assertEqual(result.attempted, 5)
        self.assertEqual(result.status, "success")
        # Frozen: cannot mutate
        with self.assertRaises(Exception):
            result.attempted = 10  # type: ignore[misc]

    def test_status_success(self):
        result = db.WriteResult(attempted=3, written=3, failed=0, errors=())
        self.assertEqual(result.status, "success")

    def test_status_partial(self):
        result = db.WriteResult(attempted=3, written=2, failed=1, errors=("boom",))
        self.assertEqual(result.status, "partial")

    def test_status_failed(self):
        result = db.WriteResult(
            attempted=3, written=0, failed=3, errors=("e1", "e2", "e3")
        )
        self.assertEqual(result.status, "failed")


# ── insert_records tests ────────────────────────────────────────────

class InsertRecordsEmptyTests(unittest.TestCase):
    """Empty-record-list edge cases."""

    def test_empty_records_returns_write_result_zeroed(self):
        result = db.insert_records("my_table", [], config={})
        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 0)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status, "success")


class InsertRecordsSuccessTests(unittest.TestCase):
    """All records succeed."""

    def test_all_records_succeed(self):
        records = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}, {"id": 3, "val": "c"}]
        session = _make_mock_session()
        with _patch_get_session(session):
            result = db.insert_records("events", records, config={"database": {}})

        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.written, 3)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status, "success")
        self.assertEqual(session.execute.call_count, 3)


class InsertRecordsPartialFailureTests(unittest.TestCase):
    """Some records fail, some succeed."""

    def test_middle_record_fails_others_succeed(self):
        records = [{"id": 1}, {"id": 2}, {"id": 3}]
        session = _make_mock_session(fail_indices={1})  # index 1 (second record)
        with _patch_get_session(session):
            result = db.insert_records("events", records, config={})

        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.written, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("simulated failure at index 1", result.errors[0])
        self.assertEqual(result.status, "partial")

    def test_first_record_fails(self):
        records = [{"id": 1}, {"id": 2}]
        session = _make_mock_session(fail_indices={0})
        with _patch_get_session(session):
            result = db.insert_records("events", records, config={})

        self.assertEqual(result.written, 1)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.status, "partial")


class InsertRecordsAllFailureTests(unittest.TestCase):
    """Every record fails."""

    def test_all_records_fail(self):
        records = [{"id": 1}, {"id": 2}]
        session = _make_mock_session(fail_indices={0, 1})
        with _patch_get_session(session):
            result = db.insert_records("events", records, config={})

        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.failed, 2)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.status, "failed")


# ── upsert_records tests ────────────────────────────────────────────


class UpsertRecordsEmptyTests(unittest.TestCase):
    """Empty-record-list edge cases."""

    def test_empty_records_returns_write_result_zeroed(self):
        result = db.upsert_records(
            "my_table", [], conflict_columns=["id"], config={}
        )
        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 0)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status, "success")


class UpsertRecordsSuccessTests(unittest.TestCase):
    """All records succeed."""

    def test_all_records_upsert_succeed(self):
        records = [
            {"id": 1, "val": "a"},
            {"id": 2, "val": "b"},
        ]
        session = _make_mock_session()
        with _patch_get_session(session):
            result = db.upsert_records(
                "events", records, conflict_columns=["id"], config={"database": {}}
            )

        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 2)
        self.assertEqual(result.written, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.status, "success")
        self.assertEqual(session.execute.call_count, 2)


class UpsertRecordsPartialFailureTests(unittest.TestCase):
    """Some upsert records fail."""

    def test_one_of_three_fails(self):
        records = [{"id": 1}, {"id": 2}, {"id": 3}]
        session = _make_mock_session(fail_indices={2})
        with _patch_get_session(session):
            result = db.upsert_records(
                "events", records, conflict_columns=["id"], config={}
            )

        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.written, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.status, "partial")


class UpsertRecordsAllFailureTests(unittest.TestCase):
    """All upsert records fail."""

    def test_all_upsert_records_fail(self):
        records = [{"id": 1}, {"id": 2}, {"id": 3}]
        session = _make_mock_session(fail_indices={0, 1, 2})
        with _patch_get_session(session):
            result = db.upsert_records(
                "events", records, conflict_columns=["id"], config={}
            )

        self.assertIsInstance(result, db.WriteResult)
        self.assertEqual(result.attempted, 3)
        self.assertEqual(result.written, 0)
        self.assertEqual(result.failed, 3)
        self.assertEqual(len(result.errors), 3)
        self.assertEqual(result.status, "failed")


# ── upsert_update_clause tests ──────────────────────────────────────

class UpsertRecordsUpdateClauseTests(unittest.TestCase):
    """Verify SQL generation when non-conflict columns exist."""

    def test_update_clause_generated_for_extra_columns(self):
        """When update_cols is non-empty, ON CONFLICT DO UPDATE is used."""
        records = [{"id": 1, "val": "x", "status": "new"}]
        session = _make_mock_session()
        with _patch_get_session(session):
            db.upsert_records(
                "events", records, conflict_columns=["id"], config={}
            )

        # The generated SQL should contain ON CONFLICT ... DO UPDATE SET
        executed_sql = str(session.execute.call_args.args[0])
        self.assertIn("ON CONFLICT", executed_sql)
        self.assertIn("DO UPDATE SET", executed_sql)
        self.assertIn("val = EXCLUDED.val", executed_sql)
        self.assertIn("status = EXCLUDED.status", executed_sql)

    def test_do_nothing_when_no_update_columns(self):
        """When all columns are conflict columns, ON CONFLICT DO NOTHING."""
        records = [{"id": 1}]
        session = _make_mock_session()
        with _patch_get_session(session):
            db.upsert_records(
                "events", records, conflict_columns=["id"], config={}
            )

        executed_sql = str(session.execute.call_args.args[0])
        self.assertIn("ON CONFLICT", executed_sql)
        self.assertIn("DO NOTHING", executed_sql)


if __name__ == "__main__":
    unittest.main()
