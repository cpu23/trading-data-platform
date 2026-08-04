import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import migrate

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DURABLE_JOBS_MIGRATION = REPOSITORY_ROOT / "db" / "migrations" / "011_durable_jobs.sql"
FRED_METADATA_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "012_macro_series_metadata.sql"
)
PROCESSOR_FINGERPRINT_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "013_processor_input_fingerprints.sql"
)
NEWS_RUN_LINEAGE_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "014_news_run_lineage.sql"
)
RAW_TABLES_INIT = REPOSITORY_ROOT / "db" / "init" / "002_raw_tables.sql"
CYCLE_RUNS_INIT = REPOSITORY_ROOT / "db" / "init" / "005_cycle_runs.sql"
SYSTEM_TABLES_INIT = REPOSITORY_ROOT / "db" / "init" / "004_system_tables.sql"


class AppliedMigrationTests(unittest.TestCase):
    def test_get_applied_migrations_returns_checksums_including_null(self):
        session = MagicMock()
        session.execute.return_value = [("001", "abc123"), ("002", None)]
        session_context = MagicMock()
        session_context.__enter__.return_value = session

        with patch.object(migrate, "get_session", return_value=session_context):
            applied = migrate.get_applied_migrations({"database": {}})

        self.assertEqual(applied, {"001": "abc123", "002": None})
        statement = str(session.execute.call_args.args[0])
        self.assertEqual(statement, "SELECT version, checksum FROM schema_migrations")


class MigrationApplicationTests(unittest.TestCase):
    def test_pending_migration_applies_exactly_once_across_rerun(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(migrations_dir, "001_pending.sql")
            migration_path.write_text("SELECT 1;\n")
            checksum = migrate.compute_checksum(migration_path)
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    side_effect=[{}, {"001": checksum}],
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                self.assertEqual(migrate.run_migrations({}), ["001"])
                self.assertEqual(migrate.run_migrations({}), [])

            apply_migration.assert_called_once_with("001", str(migration_path), {})

    def test_apply_migration_records_computed_checksum_transactionally(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(migrations_dir, "001_pending.sql")
            migration_path.write_text("SELECT 1;\n")
            session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = session

            with patch.object(migrate, "get_session", return_value=session_context):
                migrate.apply_migration("001", str(migration_path), {})

            insert_call = session.execute.call_args_list[1]
            self.assertEqual(
                str(insert_call.args[0]),
                "INSERT INTO schema_migrations (version, checksum) "
                "VALUES (:version, :checksum)",
            )
            self.assertEqual(
                insert_call.args[1],
                {
                    "version": "001",
                    "checksum": migrate.compute_checksum(migration_path),
                },
            )


class MigrationInventoryTests(unittest.TestCase):
    def test_repository_inventory_includes_011_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("011", versions)
        self.assertEqual(
            [int(version) for version in versions],
            sorted(int(version) for version in versions),
        )

    def test_duplicate_disk_versions_fail_clearly(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            Path(migrations_dir, "001_first.sql").write_text("SELECT 1;\n")
            Path(migrations_dir, "001_second.sql").write_text("SELECT 2;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(migrate, "get_applied_migrations", return_value={}),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "duplicate.*001.*001_first.sql.*001_second.sql"
                ):
                    migrate.run_migrations({})

            apply_migration.assert_not_called()

    def test_null_historical_checksum_backfills_only_with_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            applied_path = Path(migrations_dir, "001_applied.sql")
            applied_path.write_text("SELECT 1;\n")
            session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = session
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    return_value={"001": None},
                ),
                patch.object(migrate, "get_session", return_value=session_context),
                patch.object(migrate.logger, "info") as log_info,
            ):
                result = migrate.run_migrations({}, allow_checksum_backfill=True)

            self.assertEqual(result, [])
            statement, params = session.execute.call_args.args
            self.assertEqual(
                str(statement),
                "UPDATE schema_migrations SET checksum = :checksum "
                "WHERE version = :version AND checksum IS NULL",
            )
            self.assertEqual(
                params,
                {
                    "version": "001",
                    "checksum": migrate.compute_checksum(applied_path),
                },
            )
            log_info.assert_any_call(
                "migration_checksum_backfilled",
                version="001",
                path=str(applied_path),
                checksum=migrate.compute_checksum(applied_path),
            )

    def test_null_historical_checksum_fails_by_default(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            applied_path = Path(migrations_dir, "001_applied.sql")
            applied_path.write_text("SELECT 1;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    return_value={"001": None},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "001.*null checksum"):
                    migrate.run_migrations({})

    def test_applied_checksum_mismatch_fails_before_pending_apply(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            applied_path = Path(migrations_dir, "001_applied.sql")
            applied_path.write_text("SELECT 1;\n")
            Path(migrations_dir, "002_pending.sql").write_text("SELECT 2;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    return_value={"001": "wrong-checksum"},
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                with self.assertRaisesRegex(RuntimeError, "001.*001_applied.sql"):
                    migrate.run_migrations({})

            apply_migration.assert_not_called()

    def test_applied_version_missing_from_disk_fails_before_pending_apply(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            Path(migrations_dir, "002_pending.sql").write_text("SELECT 2;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    return_value={"001": "stored-checksum"},
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                with self.assertRaisesRegex(RuntimeError, "001"):
                    migrate.run_migrations({})

            apply_migration.assert_not_called()

    def test_missing_migrations_directory_is_fatal(self):
        with tempfile.TemporaryDirectory() as parent:
            missing = Path(parent) / "missing"
            with (
                patch.object(migrate, "MIGRATIONS_DIR", str(missing)),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(migrate, "get_applied_migrations", return_value={}),
            ):
                with self.assertRaisesRegex(FileNotFoundError, str(missing)):
                    migrate.run_migrations({})


class NumericOrderingTests(unittest.TestCase):
    def test_pending_migrations_applied_in_numeric_not_lexicographic_order(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            Path(migrations_dir, "2_second.sql").write_text("SELECT 2;\n")
            Path(migrations_dir, "10_tenth.sql").write_text("SELECT 10;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(migrate, "get_applied_migrations", return_value={}),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                result = migrate.run_migrations({})

            self.assertEqual(result, ["2", "10"])
            calls = apply_migration.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0].args[0], "2")
            self.assertEqual(calls[1].args[0], "10")

    def test_ambiguous_zero_padded_versions_rejected(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            Path(migrations_dir, "1_one.sql").write_text("SELECT 1;\n")
            Path(migrations_dir, "001_padded.sql").write_text("SELECT 1;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(migrate, "get_applied_migrations", return_value={}),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                    migrate.run_migrations({})

            apply_migration.assert_not_called()

    def test_zero_padded_migrations_accepted_when_consistent(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            Path(migrations_dir, "001_first.sql").write_text("SELECT 1;\n")
            Path(migrations_dir, "002_second.sql").write_text("SELECT 2;\n")
            Path(migrations_dir, "010_tenth.sql").write_text("SELECT 10;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(migrate, "get_applied_migrations", return_value={}),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                result = migrate.run_migrations({})

            self.assertEqual(result, ["001", "002", "010"])
            calls = apply_migration.call_args_list
            self.assertEqual(len(calls), 3)
            self.assertEqual([c.args[0] for c in calls], ["001", "002", "010"])


class AllOrNothingValidationTests(unittest.TestCase):
    def test_null_checksum_backfill_deferred_when_later_mismatch_exists(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            applied_path_1 = Path(migrations_dir, "001_applied.sql")
            applied_path_1.write_text("SELECT 1;\n")
            applied_path_2 = Path(migrations_dir, "002_applied.sql")
            applied_path_2.write_text("SELECT 2;\n")
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    return_value={"001": None, "002": "wrong-checksum"},
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
                patch.object(migrate, "backfill_checksum") as backfill_checksum,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "(?i)checksum mismatch.*002"
                ) as ctx:
                    migrate.run_migrations({}, allow_checksum_backfill=True)

            # No backfill of 001 occurred — the 002 mismatch blocked it
            backfill_checksum.assert_not_called()
            apply_migration.assert_not_called()
            # The combined error should contain the 002 issue but not an
            # error about 001 (which is a backfill candidate, not a violation).
            self.assertIn("002", str(ctx.exception))
            self.assertIn("Checksum mismatch", str(ctx.exception))

    def test_all_or_nothing_fails_combined_with_missing_file_and_mismatch(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            applied_path_2 = Path(migrations_dir, "002_applied.sql")
            applied_path_2.write_text("SELECT 2;\n")
            # version "001" is applied but missing from disk
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    return_value={"001": "old-checksum", "002": "wrong-checksum"},
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
                patch.object(migrate, "backfill_checksum") as backfill_checksum,
            ):
                with self.assertRaisesRegex(RuntimeError, "001.*missing") as ctx:
                    migrate.run_migrations({})

            backfill_checksum.assert_not_called()
            apply_migration.assert_not_called()
            self.assertIn("002", str(ctx.exception))
            self.assertIn("Checksum mismatch", str(ctx.exception))


class DurableJobsSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_clean_bootstrap_has_durable_job_columns_and_lifecycle_statuses(self):
        sql = self._sql(CYCLE_RUNS_INIT)

        self.assertIn("accepted_at timestamptz not null", sql)
        self.assertIn("started_at timestamptz,", sql)
        self.assertIn("heartbeat_at timestamptz,", sql)
        self.assertIn("worker_id text,", sql)
        self.assertIn("idempotency_key text,", sql)
        self.assertIn(
            "status in ('accepted', 'running', 'completed', 'failed', 'abandoned')",
            sql,
        )
        self.assertIn(
            "create unique index if not exists idx_cycle_runs_idempotency_key "
            "on cycle_runs (idempotency_key) where idempotency_key is not null",
            sql,
        )

    def test_upgrade_adds_and_backfills_columns_before_enforcing_nullability(self):
        sql = self._sql(DURABLE_JOBS_MIGRATION)

        add_columns = sql.index("add column if not exists accepted_at timestamptz")
        accepted_backfill = sql.index(
            "set accepted_at = coalesce(started_at, current_timestamp) "
            "where accepted_at is null"
        )
        heartbeat_backfill = sql.index(
            "set heartbeat_at = started_at where status = 'running' "
            "and heartbeat_at is null"
        )
        accepted_not_null = sql.index("alter column accepted_at set not null")
        started_nullable = sql.index("alter column started_at drop not null")

        self.assertLess(add_columns, accepted_backfill)
        self.assertLess(accepted_backfill, accepted_not_null)
        self.assertLess(heartbeat_backfill, accepted_not_null)
        self.assertLess(accepted_not_null, started_nullable)
        for column in (
            "heartbeat_at timestamptz",
            "worker_id text",
            "idempotency_key text",
        ):
            self.assertIn(f"add column if not exists {column}", sql)

    def test_upgrade_replaces_status_constraint_without_unconstrained_window(self):
        sql = self._sql(DURABLE_JOBS_MIGRATION)
        expanded = (
            "add constraint cycle_runs_status_check_expanded check "
            "(status in ('accepted', 'running', 'completed', 'failed', 'abandoned')) not valid"
        )

        add_expanded = sql.index(expanded)
        validate_expanded = sql.index(
            "validate constraint cycle_runs_status_check_expanded"
        )
        drop_old = sql.index("drop constraint cycle_runs_status_check")
        rename_expanded = sql.index(
            "rename constraint cycle_runs_status_check_expanded to cycle_runs_status_check"
        )

        self.assertLess(add_expanded, validate_expanded)
        self.assertLess(validate_expanded, drop_old)
        self.assertLess(drop_old, rename_expanded)

    def test_upgrade_creates_partial_unique_idempotency_index_without_data_deletion(
        self,
    ):
        sql = self._sql(DURABLE_JOBS_MIGRATION)

        self.assertIn(
            "create unique index if not exists idx_cycle_runs_idempotency_key "
            "on cycle_runs (idempotency_key) where idempotency_key is not null",
            sql,
        )
        for destructive_statement in (
            "drop table",
            "drop column",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)


class FredMetadataSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_bootstrap_and_idempotent_upgrade_define_matching_metadata_table(self):
        expected = (
            "macro_series_metadata ( series_id text primary key, title text, units text, "
            "seasonal_adjustment text, frequency text, fetched_at timestamptz not null )"
        )

        init_sql = self._sql(RAW_TABLES_INIT)
        migration_sql = self._sql(FRED_METADATA_MIGRATION)

        self.assertIn(f"create table {expected}", init_sql)
        self.assertIn(f"create table if not exists {expected}", migration_sql)
        for destructive in ("drop table", "drop column", "delete from", "truncate"):
            self.assertNotIn(destructive, migration_sql)


class ProcessorFingerprintSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_bootstrap_and_upgrade_match_fingerprint_history_columns_and_index(self):
        init_sql = self._sql(SYSTEM_TABLES_INIT)
        migration_sql = self._sql(PROCESSOR_FINGERPRINT_MIGRATION)
        for definition in (
            "input_fingerprint text",
            "skip_reason text",
            "forced boolean not null default false",
        ):
            self.assertIn(definition, init_sql)
            self.assertIn(f"add column if not exists {definition}", migration_sql)
        index = (
            "create index if not exists idx_processing_log_reusable_fingerprint "
            "on processing_log (processor, completed_at desc) include (input_fingerprint, output_id) "
            "where status = 'success' and input_fingerprint is not null"
        )
        self.assertIn(index, init_sql)
        self.assertIn(index, migration_sql)
        for destructive in ("drop table", "drop column", "delete from", "truncate"):
            self.assertNotIn(destructive, migration_sql)


class NewsRunLineageSchemaTests(unittest.TestCase):
    def test_bootstrap_and_idempotent_upgrade_allow_news_run_kind(self):
        init_sql = " ".join(CYCLE_RUNS_INIT.read_text().lower().split())
        migration_sql = " ".join(NEWS_RUN_LINEAGE_MIGRATION.read_text().lower().split())
        expected = "run_kind in ('cycle', 'collector', 'processor', 'news')"
        self.assertIn(expected, init_sql)
        self.assertIn(expected, migration_sql)
        self.assertIn("not valid", migration_sql)
        self.assertIn("validate constraint", migration_sql)
        for destructive in ("drop table", "drop column", "delete from", "truncate"):
            self.assertNotIn(destructive, migration_sql)


if __name__ == "__main__":
    unittest.main()
