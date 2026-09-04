import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collectors.base import CollectionWriteBatch

import db

CONFIG = {"database": {"password": "DB_PASSWORD_SENTINEL"}}


def _call_write(kind, records, *, config=CONFIG):
    if kind == "insert":
        return db.insert_records("events", records, config=config)
    return db.upsert_records("events", records, conflict_columns=["id"], config=config)


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
            with (
                self.subTest(kind=kind),
                patch.object(db, "get_session") as get_session,
            ):
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
                    [
                        {
                            "id": 7,
                            "payload": json.dumps({"secret": "value"}),
                            "status": "new",
                        }
                    ],
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
        self.assertIn(
            "ON CONFLICT (id) DO NOTHING", str(session.execute.call_args.args[0])
        )

    def test_insert_only_upsert_never_revises_existing_rows(self):
        # insert_only keeps the legacy single-table path immutable: a
        # conflict is a no-op and no EXCLUDED column ever updates a stored
        # row (so updated_at can never be bumped by re-collection).
        session = MagicMock()
        with patch.object(
            db, "get_session", return_value=_transaction(session, [], "batch")
        ):
            db.upsert_records(
                "events",
                [{"id": 1, "payload": {"secret": "value"}, "status": "new"}],
                ["id"],
                config=CONFIG,
                insert_only=True,
            )
        sql = str(session.execute.call_args.args[0])
        self.assertIn("ON CONFLICT (id) DO NOTHING", sql)
        self.assertNotIn("DO UPDATE", sql)
        self.assertNotIn("EXCLUDED", sql)


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
                self.assertEqual(
                    get_session.call_args_list, [call(CONFIG), call(CONFIG)]
                )
                batch.rollback.assert_called_once_with()
                batch.commit.assert_not_called()
                batch.close.assert_called_once_with()
                fallback.commit.assert_called_once_with()
                fallback.rollback.assert_not_called()
                fallback.close.assert_called_once_with()
                self.assertLess(
                    events.index("batch:close"), events.index("fallback:open")
                )
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
                self.assertEqual(
                    events, ["batch:open", "batch:rollback", "batch:close"]
                )


class WriteBatchesInSessionTests(unittest.TestCase):
    def _session(self):
        return MagicMock(name="batch_write_session")

    def test_insert_only_batch_uses_do_nothing_and_never_update(self):
        session = self._session()
        records = [{"action_id": "a1", "metadata": {"provider_event_key": "7"}}]
        batches = [
            CollectionWriteBatch(
                "corporate_actions", records, ["action_id"], insert_only=True
            )
        ]

        results = db.write_batches_in_session(session, batches)

        self.assertEqual(results, [db.WriteResult(1, 1, 0, ())])
        statement = str(session.execute.call_args.args[0])
        self.assertIn(
            "INSERT INTO corporate_actions (action_id, metadata) "
            "VALUES (:action_id, :metadata)",
            statement,
        )
        self.assertIn("ON CONFLICT (action_id) DO NOTHING", statement)
        self.assertNotIn("DO UPDATE", statement)
        params = session.execute.call_args.args[1]
        self.assertEqual(params[0]["metadata"], json.dumps({"provider_event_key": "7"}))
        session.commit.assert_not_called()

    def test_mutable_batch_keeps_existing_upsert_semantics(self):
        session = self._session()
        batches = [CollectionWriteBatch("events", [{"id": 1, "payload": "x"}], ["id"])]

        results = db.write_batches_in_session(session, batches)

        self.assertEqual(results, [db.WriteResult(1, 1, 0, ())])
        statement = str(session.execute.call_args.args[0])
        self.assertIn(
            "INSERT INTO events (id, payload) VALUES (:id, :payload)", statement
        )
        self.assertIn(
            "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload", statement
        )

    def test_source_document_upsert_preserves_first_acquisition(self):
        session = self._session()
        db.upsert_records_in_session(
            session,
            "source_documents",
            [
                {
                    "document_id": "doc-1",
                    "acquired_at": "2026-08-15T07:00:00+00:00",
                    "title": "Q2 call",
                }
            ],
            ["document_id"],
        )

        statement = str(session.execute.call_args.args[0])
        self.assertIn(
            "acquired_at = LEAST(source_documents.acquired_at, "
            "source_documents.created_at, EXCLUDED.acquired_at)",
            statement,
        )
        self.assertIn("title = EXCLUDED.title", statement)

    def test_mutable_batch_with_all_conflict_columns_uses_do_nothing(self):
        session = self._session()
        db.write_batches_in_session(
            session, [CollectionWriteBatch("events", [{"id": 1}], ["id"])]
        )
        statement = str(session.execute.call_args.args[0])
        self.assertIn("ON CONFLICT (id) DO NOTHING", statement)

    def test_sequence_returns_deterministic_per_batch_results_in_order(self):
        session = self._session()
        batches = [
            CollectionWriteBatch("events", [{"id": 1}, {"id": 2}], ["id"]),
            CollectionWriteBatch(
                "corporate_actions",
                [{"action_id": "a"}, {"action_id": "b"}, {"action_id": "c"}],
                ["action_id"],
                insert_only=True,
            ),
        ]

        results = db.write_batches_in_session(session, batches)

        self.assertEqual(
            results,
            [
                db.WriteResult(2, 2, 0, ()),
                db.WriteResult(3, 3, 0, ()),
            ],
        )
        self.assertEqual(session.execute.call_count, 2)
        self.assertIn(
            "INSERT INTO events",
            str(session.execute.call_args_list[0].args[0]),
        )
        self.assertIn(
            "INSERT INTO corporate_actions",
            str(session.execute.call_args_list[1].args[0]),
        )

    def test_identifier_validation_rejects_unsafe_identifiers_before_execution(self):
        unsafe = (
            CollectionWriteBatch("market_data; DROP TABLE events", [{"id": 1}], ["id"]),
            CollectionWriteBatch("events", [{"id": 1}], ["id; DROP TABLE events"]),
            CollectionWriteBatch(
                "events", [{"id": 1, "SELECT * FROM events": 2}], ["id"]
            ),
            CollectionWriteBatch("UPPER", [{"id": 1}], ["id"]),
        )
        for batch in unsafe:
            with self.subTest(batch=batch):
                session = self._session()
                with self.assertRaisesRegex(ValueError, "identifier"):
                    db.write_batches_in_session(session, [batch])
                session.execute.assert_not_called()

    def test_heterogeneous_schema_is_rejected_before_any_execute(self):
        session = self._session()
        batches = [
            CollectionWriteBatch("events", [{"id": 1}, {"id": 2, "extra": 3}], ["id"])
        ]

        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            db.write_batches_in_session(session, batches)

        session.execute.assert_not_called()

    def test_conflict_column_missing_from_records_is_rejected_before_execution(self):
        session = self._session()
        batches = [
            CollectionWriteBatch("events", [{"id": 1, "value": 2}], ["action_id"])
        ]

        with self.assertRaisesRegex(ValueError, "conflict column not present"):
            db.write_batches_in_session(session, batches)

        session.execute.assert_not_called()

    def test_one_error_rolls_back_all_batches(self):
        events = []
        session = self._session()
        session.execute.side_effect = [MagicMock(rowcount=1), ValueError("boom")]
        batches = [
            CollectionWriteBatch("events", [{"id": 1}], ["id"]),
            CollectionWriteBatch(
                "corporate_actions",
                [{"action_id": "a"}],
                ["action_id"],
                insert_only=True,
            ),
        ]
        with patch.object(
            db, "get_session", return_value=_transaction(session, events, "multi")
        ):
            with self.assertRaises(db.BatchWriteError) as raised:
                with db.get_session(CONFIG) as active:
                    db.write_batches_in_session(active, batches)

        self.assertEqual(raised.exception.batch_index, 1)
        self.assertEqual(raised.exception.table_name, "corporate_actions")
        self.assertEqual(raised.exception.error_type, "ValueError")
        self.assertNotIn("boom", str(raised.exception))
        self.assertEqual(session.execute.call_count, 2)
        session.commit.assert_not_called()
        session.rollback.assert_called_once_with()
        self.assertEqual(events, ["multi:open", "multi:rollback", "multi:close"])

    def test_empty_batches_return_empty_results_without_session_work(self):
        session = self._session()
        self.assertEqual(db.write_batches_in_session(session, []), [])
        session.execute.assert_not_called()

    def test_empty_records_batch_is_a_noop_with_zero_metrics(self):
        session = self._session()
        batches = [
            CollectionWriteBatch("events", [], ["id"]),
            CollectionWriteBatch(
                "corporate_actions",
                [{"action_id": "a"}],
                ["action_id"],
                insert_only=True,
            ),
        ]

        results = db.write_batches_in_session(session, batches)

        self.assertEqual(
            results,
            [db.WriteResult(0, 0, 0, ()), db.WriteResult(1, 1, 0, ())],
        )
        session.execute.assert_called_once()
        self.assertIn(
            "INSERT INTO corporate_actions", str(session.execute.call_args.args[0])
        )


class SessionCleanupBoundaryTests(unittest.TestCase):
    def _session_factory(self, *sessions):
        factory = MagicMock(side_effect=sessions)
        return patch.object(db, "_get_session_factory", return_value=factory), factory

    def test_committed_batch_close_failure_is_safe_cleanup_not_replayed(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                session = MagicMock(name=f"{kind}_committed_batch")
                session.close.side_effect = RuntimeError("RAW_CLOSE_SENTINEL")
                factory_patch, factory = self._session_factory(session)

                with factory_patch, patch.object(db.logger, "warning") as warning:
                    result = _call_write(kind, [{"id": 1}, {"id": 2}])

                self.assertEqual(result, db.WriteResult(2, 2, 0, ()))
                factory.assert_called_once_with()
                session.execute.assert_called_once()
                session.commit.assert_called_once_with()
                session.rollback.assert_not_called()
                session.close.assert_called_once_with()
                cleanup_calls = [
                    logged
                    for logged in warning.call_args_list
                    if logged.args == ("db_session_close_failed",)
                ]
                self.assertEqual(len(cleanup_calls), 1)
                self.assertEqual(cleanup_calls[0].kwargs["error_type"], "RuntimeError")
                self.assertNotIn("RAW_CLOSE_SENTINEL", repr(warning.call_args_list))
                self.assertNotIn(f"{kind}_batch_failed", repr(warning.call_args_list))

    def test_commit_failure_rolls_back_closes_and_uses_fresh_fallback(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                batch = MagicMock(name=f"{kind}_commit_failed_batch")
                batch.commit.side_effect = RuntimeError("RAW_COMMIT_SENTINEL")
                fallback = _fallback_session([])
                factory_patch, factory = self._session_factory(batch, fallback)

                with factory_patch, patch.object(db.logger, "warning") as warning:
                    result = _call_write(kind, [{"id": 1}, {"id": 2}])

                self.assertEqual(result, db.WriteResult(2, 2, 0, ()))
                self.assertEqual(factory.call_count, 2)
                self.assertIsNot(batch, fallback)
                batch.execute.assert_called_once()
                batch.commit.assert_called_once_with()
                batch.rollback.assert_called_once_with()
                batch.close.assert_called_once_with()
                self.assertEqual(fallback.execute.call_count, 2)
                fallback.commit.assert_called_once_with()
                batch_failure_calls = [
                    logged
                    for logged in warning.call_args_list
                    if logged.args == (f"{kind}_batch_failed",)
                ]
                self.assertEqual(len(batch_failure_calls), 1)
                self.assertEqual(
                    batch_failure_calls[0].kwargs["error_type"], "RuntimeError"
                )
                self.assertNotIn("RAW_COMMIT_SENTINEL", repr(warning.call_args_list))

    def test_execute_failure_remains_primary_when_rollback_and_close_fail(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                batch = MagicMock(name=f"{kind}_cleanup_failed_batch")
                batch.execute.side_effect = LookupError("RAW_EXECUTE_SENTINEL")
                batch.rollback.side_effect = RuntimeError("RAW_ROLLBACK_SENTINEL")
                batch.close.side_effect = OSError("RAW_CLOSE_SENTINEL")
                fallback = _fallback_session([])
                factory_patch, factory = self._session_factory(batch, fallback)

                with factory_patch, patch.object(db.logger, "warning") as warning:
                    result = _call_write(kind, [{"id": 1}, {"id": 2}])

                self.assertEqual(result, db.WriteResult(2, 2, 0, ()))
                self.assertEqual(factory.call_count, 2)
                self.assertEqual(fallback.execute.call_count, 2)
                batch.rollback.assert_called_once_with()
                batch.close.assert_called_once_with()
                batch_failure_calls = [
                    logged
                    for logged in warning.call_args_list
                    if logged.args == (f"{kind}_batch_failed",)
                ]
                self.assertEqual(len(batch_failure_calls), 1)
                self.assertEqual(
                    batch_failure_calls[0].kwargs["error_type"], "LookupError"
                )
                self.assertIn(
                    "db_session_rollback_failed", repr(warning.call_args_list)
                )
                self.assertIn("db_session_close_failed", repr(warning.call_args_list))
                self.assertNotIn("RAW_EXECUTE_SENTINEL", repr(warning.call_args_list))
                self.assertNotIn("RAW_ROLLBACK_SENTINEL", repr(warning.call_args_list))
                self.assertNotIn("RAW_CLOSE_SENTINEL", repr(warning.call_args_list))

    def test_committed_fallback_close_failure_keeps_truthful_row_counts(self):
        for kind in ("insert", "upsert"):
            with self.subTest(kind=kind):
                batch = MagicMock(name=f"{kind}_failed_batch")
                batch.execute.side_effect = RuntimeError("RAW_BATCH_SENTINEL")
                fallback = _fallback_session([])
                fallback.close.side_effect = OSError("RAW_FALLBACK_CLOSE_SENTINEL")
                factory_patch, factory = self._session_factory(batch, fallback)

                with factory_patch, patch.object(db.logger, "warning") as warning:
                    result = _call_write(kind, [{"id": 1}, {"id": 2}])

                self.assertEqual(result, db.WriteResult(2, 2, 0, ()))
                self.assertEqual(result.status, "success")
                self.assertEqual(factory.call_count, 2)
                fallback.commit.assert_called_once_with()
                fallback.rollback.assert_not_called()
                fallback.close.assert_called_once_with()
                cleanup_calls = [
                    logged
                    for logged in warning.call_args_list
                    if logged.args == ("db_session_close_failed",)
                ]
                self.assertEqual(len(cleanup_calls), 1)
                self.assertEqual(cleanup_calls[0].kwargs["error_type"], "OSError")
                self.assertNotIn(
                    "RAW_FALLBACK_CLOSE_SENTINEL", repr(warning.call_args_list)
                )


if __name__ == "__main__":
    unittest.main()
