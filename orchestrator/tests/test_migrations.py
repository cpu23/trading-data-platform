import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import migrate


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

            apply_migration.assert_called_once_with(
                "001", str(migration_path), {}
            )

    def test_apply_migration_records_computed_checksum_transactionally(self):
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(migrations_dir, "001_pending.sql")
            migration_path.write_text("SELECT 1;\n")
            session = MagicMock()
            session_context = MagicMock()
            session_context.__enter__.return_value = session

            with patch.object(
                migrate, "get_session", return_value=session_context
            ):
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
                result = migrate.run_migrations(
                    {}, allow_checksum_backfill=True
                )

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


if __name__ == "__main__":
    unittest.main()
