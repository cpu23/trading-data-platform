import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db


CONFIG = {"database": {"password": "DB_PASSWORD_SENTINEL"}}


def _call_write(kind, records, *, config=CONFIG):
    if kind == "insert":
        return db.insert_records("events", records, config=config)
    return db.upsert_records(
        "events", records, conflict_columns=["id"], config=config
    )


def _transaction(session, events, name):
    """A realistic get_session stand-in with commit/rollback/close boundaries."""

    @contextmanager
    def transaction():
        events.append(f"{name}:open")
        try:
            yield session
            session.commit()
            events.append(f"{name}:commit")
        except Exception:
            session.rollback()
            events.append(f"{name}:rollback")
            raise
        finally:
            session.close()
            events.append(f"{name}:close")

    return transaction()


def _fallback_session(events, *, failing_ids=()):
    session = MagicMock(name="fallback_session")
    failing_ids = set(failing_ids)

    @contextmanager
    def nested():
        record_id = session.execute.call_count
        events.append(f"nested:{record_id}:open")
        try:
            yield
            events.append(f"nested:{record_id}:commit")
        except Exception:
            events.append(f"nested:{record_id}:rollback")
            raise

    session.begin_nested.side_effect = nested

    def execute(_stmt, params):
        if params["id"] in failing_ids:
            raise ValueError(
                f"bad record {params!r}; DB_PASSWORD_SENTINEL; RECORD_SECRET_SENTINEL"
            )
        return MagicMock(rowcount=1)

    session.execute.side_effect = execute
    return session


class WriteResultDataclassTests(unittest.TestCase):
    def test_frozen_status_semantics(self):
        cases = [
            (db.WriteResult(0, 0, 0, ()), "success"),
            (db.WriteResult(3, 3, 0, ()), "success"),
            (db.WriteResult(3, 2, 1, ("record 2 failed",)), "partial"),
            (db.WriteResult(3, 0, 3, ("records not written",)), "failed"),
        ]
        for result, status in cases:
            with self.subTest(result=result):
                self.assertEqual(result.status, status)
        with self.assertRaises(Exception):
            cases[0][0].written = 1  # type: ignore[misc]


class BatchFirstWriteTests(unittest.TestCase):
    def test_schema_mismatch_is_rejected_before_preparation_statement_or_session(self):
        cases = (
            (
                "first record has a key missing later",
                [{"id": 1, "API_TOKEN_SENTINEL_KEY": "FIRST_SECRET"}, {"id": 2}],
            ),
            (
                "later record has an extra key",
                [{"id": 1}, {"id": 2, "API_TOKEN_SENTINEL_KEY": "LATER_SECRET"}],
            ),
        )

        for kind in ("insert", "upsert"):
            for label, records in cases:
                with self.subTest(kind=kind, case=label):
                    with (
                        patch.object(db, "_prepare_records") as prepare,
                        patch.object(db, "text") as statement,
                        patch.object(db, "get_session") as get_session,
                    ):
                        result = _call_write(kind, records)

                self.assertEqual(
                    result,
                    db.WriteResult(
                        attempted=2,
                        written=0,
                        failed=2,
                        errors=("record schema mismatch at index 1",),
                    ),
                )
                self.assertNotIn("API_TOKEN_SENTINEL_KEY", repr(result.errors))
                self.assertNotIn("FIRST_SECRET", repr(result.errors))
                self.assertNotIn("LATER_SECRET", repr(result.errors))
                prepare.assert_not_called()
                statement.assert_not_called()
                get_session.assert_not_called()

    def test_same_schema_in_different_key_order_uses_normal_executemany(self):
        records = [{"id": 1, "value": "a"}, {"value": "b", "id": 2}]

        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                session = MagicMock()
                with patch.object(
                    db, "get_session", return_value=_transaction(session, [], "batch")
                ) as get_session:
                    result = _call_write(kind, records)

                self.assertEqual(result, db.WriteResult(2, 2, 0, ()))
                session.execute.assert_called_once()
                self.assertEqual(session.execute.call_args.args[1], records)
                get_session.assert_called_once_with(CONFIG)

    def test_non_empty_success_uses_one_executemany_and_truthful_result(self):
        records = [
            {"id": 1, "payload": {"token": "alpha"}},
            {"id": 2, "payload": ["beta"]},
            {"id": 3, "payload": "plain"},
        ]
        expected_params = [
            {"id": 1, "payload": json.dumps({"token": "alpha"})},
            {"id": 2, "payload": json.dumps(["beta"])},
            {"id": 3, "payload": "plain"},
        ]

        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                events = []
                session = MagicMock(name=f"{kind}_batch_session")
                with patch.object(
                    db,
                    "get_session",
                    return_value=_transaction(session, events, "batch"),
                ) as get_session:
                    result = _call_write(kind, records)

                self.assertEqual(result, db.WriteResult(3, 3, 0, ()))
                self.assertEqual(result.status, "success")
                self.assertEqual(session.execute.call_count, 1)
                self.assertEqual(session.execute.call_args.args[1], expected_params)
                session.begin_nested.assert_not_called()
                session.commit.assert_called_once_with()
                session.rollback.assert_not_called()
                session.close.assert_called_once_with()
                get_session.assert_called_once_with(CONFIG)
                self.assertEqual(events, ["batch:open", "batch:commit", "batch:close"])

    def test_empty_records_return_zero_without_session_work(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind), patch.object(db, "get_session") as get_session:
                result = _call_write(kind, [])
            self.assertEqual(result, db.WriteResult(0, 0, 0, ()))
            get_session.assert_not_called()

    def test_statement_and_prepared_parameters_are_preserved(self):
        records = [{"id": 7, "payload": {"secret": "value"}, "status": "new"}]

        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                session = MagicMock()
                with patch.object(
                    db, "get_session", return_value=_transaction(session, [], "batch")
                ):
                    _call_write(kind, records)

                stmt, params = session.execute.call_args.args
                sql = str(stmt)
                self.assertIn(
                    "INSERT INTO events (id, payload, status) VALUES (:id, :payload, :status)",
                    sql,
                )
                self.assertEqual(
                    params,
                    [{"id": 7, "payload": json.dumps({"secret": "value"}), "status": "new"}],
                )
                if kind == "upsert":
                    self.assertIn("ON CONFLICT (id) DO UPDATE SET", sql)
                    self.assertIn("payload = EXCLUDED.payload", sql)
                    self.assertIn("status = EXCLUDED.status", sql)
                else:
                    self.assertNotIn("ON CONFLICT", sql)

    def test_upsert_all_conflict_columns_keeps_do_nothing_clause(self):
        session = MagicMock()
        with patch.object(
            db, "get_session", return_value=_transaction(session, [], "batch")
        ):
            db.upsert_records("events", [{"id": 1}], ["id"], config=CONFIG)
        self.assertIn("ON CONFLICT (id) DO NOTHING", str(session.execute.call_args.args[0]))


class DiagnosticFallbackTests(unittest.TestCase):
    def _run_fallback(self, kind, failing_ids):
        events = []
        batch_session = MagicMock(name="failed_batch_session")
        batch_session.execute.side_effect = RuntimeError(
            "batch failed DB_PASSWORD_SENTINEL RECORD_SECRET_SENTINEL"
        )
        fallback_session = _fallback_session(events, failing_ids=failing_ids)
        contexts = [
            _transaction(batch_session, events, "batch"),
            _transaction(fallback_session, events, "fallback"),
        ]
        with patch.object(db, "get_session", side_effect=contexts) as get_session:
            result = _call_write(
                kind,
                [
                    {"id": 1, "value": "first"},
                    {"id": 2, "value": "RECORD_SECRET_SENTINEL"},
                    {"id": 3, "value": "third"},
                ],
            )
        return result, events, batch_session, fallback_session, get_session

    def test_batch_failure_uses_fresh_transaction_and_isolates_partial_failures(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                result, events, batch, fallback, get_session = self._run_fallback(
                    kind, failing_ids={2}
                )

                self.assertEqual(result.attempted, 3)
                self.assertEqual(result.written, 2)
                self.assertEqual(result.failed, 1)
                self.assertEqual(result.status, "partial")
                self.assertEqual(len(result.errors), 1)
                self.assertIn("record 2", result.errors[0])
                self.assertNotIn("batch failed", " ".join(result.errors))
                self.assertNotIn("RECORD_SECRET_SENTINEL", " ".join(result.errors))
                self.assertNotIn("DB_PASSWORD_SENTINEL", " ".join(result.errors))

                self.assertEqual(batch.execute.call_count, 1)
                self.assertIsInstance(batch.execute.call_args.args[1], list)
                self.assertEqual(fallback.execute.call_count, 3)
                self.assertEqual(fallback.begin_nested.call_count, 3)
                self.assertIsNot(batch, fallback)
                self.assertEqual(get_session.call_args_list, [call(CONFIG), call(CONFIG)])
                batch.rollback.assert_called_once_with()
                batch.commit.assert_not_called()
                batch.close.assert_called_once_with()
                fallback.commit.assert_called_once_with()
                fallback.rollback.assert_not_called()
                fallback.close.assert_called_once_with()
                self.assertLess(events.index("batch:close"), events.index("fallback:open"))
                self.assertIn("nested:1:rollback", events)

    def test_batch_failure_then_all_rows_fail_counts_only_isolated_rows(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                result, _events, _batch, fallback, _get_session = self._run_fallback(
                    kind, failing_ids={1, 2, 3}
                )
                self.assertEqual(result, db.WriteResult(3, 0, 3, result.errors))
                self.assertEqual(result.status, "failed")
                self.assertEqual(len(result.errors), 3)
                self.assertEqual(fallback.execute.call_count, 3)
                self.assertNotIn("batch failed", " ".join(result.errors))

    def test_fallback_session_creation_failure_is_clear_safe_and_truthful(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                events = []
                batch = MagicMock()
                batch.execute.side_effect = RuntimeError("batch RECORD_SECRET_SENTINEL")
                first_context = _transaction(batch, events, "batch")
                calls = [0]

                def get_session(_config):
                    calls[0] += 1
                    if calls[0] == 1:
                        return first_context
                    raise ConnectionError(
                        "password=SQL_CREDENTIAL_SENTINEL RECORD_SECRET_SENTINEL"
                    )

                with patch.object(db, "get_session", side_effect=get_session):
                    result = _call_write(kind, [{"id": 1}, {"id": 2}])

                self.assertEqual(result.attempted, 2)
                self.assertEqual(result.written, 0)
                self.assertEqual(result.failed, 2)
                self.assertEqual(result.status, "failed")
                self.assertEqual(len(result.errors), 1)
                self.assertIn("diagnostic fallback unavailable", result.errors[0])
                self.assertNotIn("SQL_CREDENTIAL_SENTINEL", result.errors[0])
                self.assertNotIn("DB_PASSWORD_SENTINEL", result.errors[0])
                self.assertNotIn("RECORD_SECRET_SENTINEL", result.errors[0])
                batch.rollback.assert_called_once_with()
                batch.close.assert_called_once_with()
                self.assertEqual(events, ["batch:open", "batch:rollback", "batch:close"])


if __name__ == "__main__":
    unittest.main()
