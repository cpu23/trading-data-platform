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
AUTONOMOUS_THESIS_DESK_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "049_autonomous_thesis_desk.sql"
)
FREE_MARKET_SOURCES_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "050_free_market_sources.sql"
)
EVENT_PLAYBOOKS_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "051_thesis_event_playbooks.sql"
)
OPTION_SNAPSHOT_FEATURES_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "052_option_snapshot_features.sql"
)
FORECAST_SCENARIO_UNIQUENESS_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "053_forecast_scenario_uniqueness.sql"
)
CATALYST_IMMUTABILITY_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "054_catalyst_immutability.sql"
)
THESIS_FUSION_REFERENCE_MIGRATION = (
    REPOSITORY_ROOT / "db" / "migrations" / "055_thesis_fusion_reference.sql"
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

    def test_event_playbook_migration_applies_exactly_once_across_rerun(self):
        # The real 051 file must survive the clean-migration rerun gate:
        # re-applying it is a no-op (every statement guarded), so the second
        # run reports no pending migrations.
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(migrations_dir, "051_thesis_event_playbooks.sql")
            migration_path.write_text(EVENT_PLAYBOOKS_MIGRATION.read_text())
            checksum = migrate.compute_checksum(migration_path)
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    side_effect=[{}, {"051": checksum}],
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                self.assertEqual(migrate.run_migrations({}), ["051"])
                self.assertEqual(migrate.run_migrations({}), [])

            apply_migration.assert_called_once_with("051", str(migration_path), {})

    def test_forecast_scenario_migration_applies_exactly_once_across_rerun(self):
        # The real 053 file must survive the clean-migration rerun gate:
        # re-applying it is a no-op (the dedupe UPDATE touches nothing once
        # no active duplicates remain, and the index creation is guarded),
        # so the second run reports no pending migrations.
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(
                migrations_dir, "053_forecast_scenario_uniqueness.sql"
            )
            migration_path.write_text(
                FORECAST_SCENARIO_UNIQUENESS_MIGRATION.read_text()
            )
            checksum = migrate.compute_checksum(migration_path)
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    side_effect=[{}, {"053": checksum}],
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                self.assertEqual(migrate.run_migrations({}), ["053"])
                self.assertEqual(migrate.run_migrations({}), [])

            apply_migration.assert_called_once_with("053", str(migration_path), {})

    def test_catalyst_immutability_migration_applies_exactly_once_across_rerun(self):
        # The real 054 file must survive the clean-migration rerun gate:
        # re-applying it is a no-op (the legacy stamp is guarded by the
        # trigger's existence, the function is CREATE OR REPLACE, and the
        # trigger is dropped/recreated), so the second run reports no
        # pending migrations.
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(migrations_dir, "054_catalyst_immutability.sql")
            migration_path.write_text(CATALYST_IMMUTABILITY_MIGRATION.read_text())
            checksum = migrate.compute_checksum(migration_path)
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    side_effect=[{}, {"054": checksum}],
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                self.assertEqual(migrate.run_migrations({}), ["054"])
                self.assertEqual(migrate.run_migrations({}), [])

            apply_migration.assert_called_once_with("054", str(migration_path), {})

    def test_thesis_fusion_reference_migration_applies_exactly_once_across_rerun(
        self,
    ):
        # The real 055 file must survive the clean-migration rerun gate:
        # re-applying it is a no-op (both columns are ADD COLUMN IF NOT
        # EXISTS and the legacy reference backfill is self-guarding on
        # fusion_reference_at IS NULL), so the second run reports no
        # pending migrations.
        with tempfile.TemporaryDirectory() as migrations_dir:
            migration_path = Path(migrations_dir, "055_thesis_fusion_reference.sql")
            migration_path.write_text(THESIS_FUSION_REFERENCE_MIGRATION.read_text())
            checksum = migrate.compute_checksum(migration_path)
            with (
                patch.object(migrate, "MIGRATIONS_DIR", migrations_dir),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(
                    migrate,
                    "get_applied_migrations",
                    side_effect=[{}, {"055": checksum}],
                ),
                patch.object(migrate, "apply_migration") as apply_migration,
            ):
                self.assertEqual(migrate.run_migrations({}), ["055"])
                self.assertEqual(migrate.run_migrations({}), [])

            apply_migration.assert_called_once_with("055", str(migration_path), {})

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

    def test_repository_inventory_includes_049_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("049", versions)
        self.assertEqual(
            [int(version) for version in versions],
            sorted(int(version) for version in versions),
        )

    def test_repository_inventory_includes_050_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("050", versions)
        self.assertEqual(
            [int(version) for version in versions],
            sorted(int(version) for version in versions),
        )

    def test_repository_inventory_includes_051_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("051", versions)
        self.assertEqual(
            [int(version) for version in versions],
            sorted(int(version) for version in versions),
        )

    def test_repository_inventory_includes_053_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("053", versions)
        self.assertEqual(
            [int(version) for version in versions],
            sorted(int(version) for version in versions),
        )

    def test_repository_inventory_includes_054_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("054", versions)
        self.assertEqual(
            [int(version) for version in versions],
            sorted(int(version) for version in versions),
        )

    def test_repository_inventory_includes_055_in_numeric_order(self):
        migrations = sorted(
            (REPOSITORY_ROOT / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [path.name.split("_", 1)[0] for path in migrations]

        self.assertIn("055", versions)
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


class AutonomousThesisDeskSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_upgrade_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        # The single permitted "drop constraint" is the guarded, transactional
        # swap of the evidence relationship check (to admit 'invalidation');
        # no other constraint drops are allowed.
        self.assertEqual(sql.count("drop constraint"), 1)
        self.assertIn(
            "drop constraint if exists investment_thesis_evidence_relationship_check",
            sql,
        )
        for table in (
            "investment_thesis_groups",
            "investment_thesis_group_members",
            "investment_thesis_scenarios",
            "investment_thesis_forecasts",
            "investment_forecast_outcomes",
            "investment_opportunity_snapshots",
            "investment_thesis_falsification_runs",
            "position_thesis_links",
        ):
            self.assertIn(f"create table if not exists {table}", sql)

    def test_groups_created_before_thesis_group_id_references_them(self):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        groups = sql.index("create table if not exists investment_thesis_groups")
        group_id = sql.index(
            "add column if not exists group_id uuid "
            "references investment_thesis_groups (id) on delete set null"
        )
        self.assertLess(groups, group_id)

    def test_theses_gain_neutral_default_autonomy_columns_with_bounded_checks(
        self,
    ):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        for column in (
            "add column if not exists group_id uuid "
            "references investment_thesis_groups (id) on delete set null",
            "add column if not exists origin text not null default 'manual' "
            "check (origin in ('manual', 'generated', 'fusion'))",
            "add column if not exists canonical_key text,",
            "add column if not exists mechanism text,",
            "add column if not exists direction text not null default 'neutral' "
            "check (direction in ('long', 'short', 'neutral'))",
            "add column if not exists catalyst_summary text,",
            "add column if not exists evidence_strength double precision "
            "not null default 0 check (evidence_strength between 0 and 1),",
            "add column if not exists contradiction_strength double precision "
            "not null default 0 check (contradiction_strength between 0 and 1),",
            "add column if not exists neglect_score double precision "
            "not null default 0 check (neglect_score between 0 and 1),",
            "add column if not exists catalyst_score double precision "
            "not null default 0 check (catalyst_score between 0 and 1),",
            "add column if not exists confidence_score double precision "
            "not null default 0 check (confidence_score between 0 and 1),",
            "add column if not exists expected_value double precision "
            "not null default 0,",
            "add column if not exists expected_shortfall double precision "
            "not null default 0,",
            "add column if not exists opportunity_score double precision "
            "not null default 0 check (opportunity_score between 0 and 1),",
            "add column if not exists last_evaluated_at timestamptz,",
            "add column if not exists last_evidence_at timestamptz,",
            "add column if not exists input_fingerprint text;",
        ):
            self.assertIn(column, sql)
        self.assertIn(
            "create unique index if not exists idx_investment_theses_canonical_key "
            "on investment_theses (canonical_key) where canonical_key is not null",
            sql,
        )
        self.assertIn(
            "create unique index if not exists idx_investment_theses_input_fingerprint "
            "on investment_theses (input_fingerprint) "
            "where input_fingerprint is not null",
            sql,
        )

    def test_evidence_gains_provenance_and_weight_columns_without_new_primary_key(
        self,
    ):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        for column in (
            "add column if not exists source_family text not null default 'manual',",
            "add column if not exists origin_key text,",
            "add column if not exists independence_key text,",
            "add column if not exists evidence_fingerprint text,",
            "add column if not exists source_timestamp timestamptz,",
            "add column if not exists available_at timestamptz,",
            "add column if not exists quality_score double precision "
            "not null default 0 check (quality_score between 0 and 1),",
            "add column if not exists entailment_score double precision "
            "not null default 0 check (entailment_score between 0 and 1),",
            "add column if not exists freshness_score double precision "
            "not null default 0 check (freshness_score between 0 and 1),",
            "add column if not exists effective_weight double precision "
            "not null default 1 check (effective_weight between 0 and 1);",
        ):
            self.assertIn(column, sql)
        # Dedup/cap by independence_key; NULL keys (manual rows) exempt
        self.assertIn(
            "create unique index if not exists idx_investment_thesis_evidence_independence "
            "on investment_thesis_evidence (thesis_id, independence_key) "
            "where independence_key is not null",
            sql,
        )
        # Invalidation is a first-class relationship: the canonical check is
        # swapped under its original name (011-style: new constraint added
        # NOT VALID, validated, old dropped, new renamed into place).
        self.assertIn(
            "add constraint investment_thesis_evidence_relationship_check_v2 "
            "check (relationship in "
            "('supports', 'contradicts', 'context', 'invalidation')) not valid",
            sql,
        )
        self.assertIn(
            "validate constraint investment_thesis_evidence_relationship_check_v2",
            sql,
        )
        self.assertIn(
            "rename constraint investment_thesis_evidence_relationship_check_v2 "
            "to investment_thesis_evidence_relationship_check",
            sql,
        )
        swap = sql.index(
            "add constraint investment_thesis_evidence_relationship_check_v2"
        )
        self.assertLess(swap, sql.index("validate constraint"))
        self.assertLess(
            sql.index("validate constraint"),
            sql.index(
                "drop constraint if exists investment_thesis_evidence_relationship_check"
            ),
        )
        self.assertLess(
            sql.index(
                "drop constraint if exists investment_thesis_evidence_relationship_check"
            ),
            sql.index(
                "rename constraint investment_thesis_evidence_relationship_check_v2"
            ),
        )
        # The composite primary key is preserved: the only constraint the
        # evidence section adds is the relationship swap, and no statement
        # defines a primary key.
        evidence_alter = sql[sql.index("alter table investment_thesis_evidence") :]
        evidence_alter = evidence_alter[
            : evidence_alter.index(
                "create table if not exists investment_thesis_group_members"
            )
        ]
        self.assertEqual(evidence_alter.count("add constraint"), 1)
        self.assertIn(
            "add constraint investment_thesis_evidence_relationship_check_v2",
            evidence_alter,
        )
        self.assertNotIn("add primary key", evidence_alter)

    def test_scenarios_are_versioned_with_nullable_probability_and_bounded_expected_return(
        self,
    ):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        # The bounded probability check stays (SQL CHECKs pass NULL), while
        # NOT NULL is dropped: unknown probability is representable and is
        # never defaulted to conviction.
        self.assertIn("check (probability between 0 and 1)", sql)
        self.assertIn(
            "probability double precision check (probability between 0 and 1)",
            sql,
        )
        self.assertIn(
            "alter table investment_thesis_scenarios "
            "alter column probability drop not null",
            sql,
        )
        # Expected return is stored, bounded and finite: the +/-100 cap
        # matches the domain's MAX_ABS_RETURN and BETWEEN rejects
        # NaN/Infinity.
        self.assertIn(
            "expected_return double precision not null default 0 "
            "check (expected_return between -100 and 100)",
            sql,
        )
        self.assertIn(
            "add column if not exists expected_return double precision "
            "not null default 0 check (expected_return between -100 and 100)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_scenarios_identity_unique "
            "unique (thesis_id, name, version)",
            sql,
        )
        self.assertIn(
            "create unique index if not exists idx_investment_thesis_scenarios_active "
            "on investment_thesis_scenarios (thesis_id, name) "
            "where superseded_at is null",
            sql,
        )
        self.assertIn(
            "create unique index if not exists idx_investment_thesis_scenarios_base_case "
            "on investment_thesis_scenarios (thesis_id) "
            "where is_base_case and superseded_at is null",
            sql,
        )

    def test_forecasts_and_outcomes_have_stable_point_in_time_identity(self):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        forecasts = (
            "create table if not exists investment_thesis_forecasts ( "
            "id uuid primary key default gen_random_uuid(), "
            "thesis_id uuid not null references investment_theses (id) "
            "on delete cascade, "
            "scenario_id uuid references investment_thesis_scenarios (id) "
            "on delete set null, "
            "forecast_key text not null, "
            "forecast_type text not null default 'price', "
            "direction text not null default 'up', "
            "target_value double precision, "
            "target_date date, "
            "as_of timestamptz not null default now(), "
            "version integer not null default 1, "
            "superseded_at timestamptz, "
            "created_at timestamptz not null default now(), "
            "constraint investment_thesis_forecasts_version_check "
            "check (version >= 1), "
            "constraint investment_thesis_forecasts_direction_check "
            "check (direction in ('up', 'down', 'flat')), "
            "constraint investment_thesis_forecasts_type_check "
            "check (forecast_type in "
            "('price', 'earnings', 'revenue', 'relative', 'other')), "
            "constraint investment_thesis_forecasts_superseded_after_created "
            "check (superseded_at is null or superseded_at >= created_at), "
            "constraint investment_thesis_forecasts_identity_unique "
            "unique (forecast_key, version) )"
        )
        outcomes = (
            "create table if not exists investment_forecast_outcomes ( "
            "id uuid primary key default gen_random_uuid(), "
            "forecast_id uuid not null references investment_thesis_forecasts (id) "
            "on delete cascade, "
            "status text not null, "
            "actual_value double precision, "
            "measured_at timestamptz not null default now(), "
            "notes text, "
            "created_at timestamptz not null default now(), "
            "constraint investment_forecast_outcomes_status_check "
            "check (status in ('hit', 'miss', 'inconclusive')), "
            "constraint investment_forecast_outcomes_forecast_unique "
            "unique (forecast_id) )"
        )
        self.assertIn(forecasts, sql)
        self.assertIn(outcomes, sql)
        self.assertIn(
            "create unique index if not exists idx_investment_thesis_forecasts_active "
            "on investment_thesis_forecasts (forecast_key) "
            "where superseded_at is null",
            sql,
        )
        self.assertIn(
            "constraint investment_forecast_outcomes_forecast_unique "
            "unique (forecast_id)",
            sql,
        )

    def test_membership_falsification_and_position_links_enforce_identity(self):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        self.assertIn(
            "create unique index if not exists idx_investment_thesis_group_members_active "
            "on investment_thesis_group_members (group_id, thesis_id) "
            "where removed_at is null",
            sql,
        )
        self.assertIn(
            "check (status in ( 'pending', 'in_progress', 'not_falsified', "
            "'falsified', 'inconclusive' ))",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_falsification_runs_identity_unique "
            "unique (thesis_id, run_key)",
            sql,
        )
        self.assertIn(
            "constraint position_thesis_links_removed_after_created "
            "check (removed_at is null or removed_at >= created_at)",
            sql,
        )
        self.assertIn(
            "create unique index if not exists idx_position_thesis_links_active "
            "on position_thesis_links (position_id, thesis_id, link_type) "
            "where removed_at is null",
            sql,
        )
        self.assertIn(
            "check (link_type in ('primary', 'secondary', 'hedge', 'watch'))",
            sql,
        )
        self.assertIn(
            "check (status in ('active', 'archived'))",
            sql,
        )

    def test_append_only_and_lifecycle_triggers_are_installed(self):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        self.assertIn(
            "create or replace function reject_thesis_immutable_mutation()", sql
        )
        for trigger in (
            "investment_thesis_forecasts_lifecycle",
            "investment_forecast_outcomes_immutable",
            "investment_opportunity_snapshots_immutable",
            "investment_thesis_group_members_append_only",
            "investment_thesis_falsification_runs_lifecycle",
            "position_thesis_links_append_only",
        ):
            self.assertIn(f"create trigger {trigger}", sql)
            self.assertIn(f"drop trigger if exists {trigger}", sql)
        # Outcomes and snapshots are strictly append-only: their triggers
        # reject every UPDATE and DELETE via the blanket immutable-mutation
        # function, leaving no lifecycle update path at all.
        self.assertIn(
            "raise exception '% is immutable'",
            sql,
        )
        for table in (
            "investment_forecast_outcomes",
            "investment_opportunity_snapshots",
        ):
            self.assertIn(
                f"create trigger {table}_immutable before update or delete "
                f"on {table} for each row execute function "
                "reject_thesis_immutable_mutation()",
                sql,
            )
        self.assertIn(
            "create or replace function enforce_thesis_forecast_lifecycle()",
            sql,
        )
        self.assertIn(
            "create or replace function enforce_thesis_group_membership_append_only()",
            sql,
        )
        self.assertIn(
            "create or replace function enforce_thesis_position_link_append_only()",
            sql,
        )
        self.assertIn(
            "create or replace function enforce_thesis_falsification_run_lifecycle()",
            sql,
        )

    def test_forecast_lifecycle_permits_only_the_one_time_supersede_transition(
        self,
    ):
        sql = self._sql(AUTONOMOUS_THESIS_DESK_MIGRATION)
        lifecycle = sql[
            sql.index(
                "create or replace function enforce_thesis_forecast_lifecycle()"
            ) :
        ]
        lifecycle = lifecycle[
            : lifecycle.index(
                "create or replace function enforce_thesis_group_membership_append_only()"
            )
        ]
        # Deletes are rejected outright.
        self.assertIn(
            "if tg_op = 'delete' then raise exception 'forecasts are append-only'",
            lifecycle,
        )
        # Identity and content (including the row id) stay frozen, so the
        # supersede transition cannot smuggle in any other change.
        self.assertIn("new.id is distinct from old.id", lifecycle)
        content_guard = lifecycle.index(
            "raise exception 'forecast content is immutable; supersede to revise'"
        )
        # An UPDATE that does not set superseded_at (revision in place) is
        # rejected: the transition must be NULL -> non-NULL.
        null_guard = lifecycle.index(
            "if new.superseded_at is null then "
            "raise exception 'forecast rows are immutable; supersede to revise'"
        )
        # A second transition on an already-superseded row is rejected:
        # version replacement happens exactly once.
        frozen_guard = lifecycle.index(
            "if old.superseded_at is not null then "
            "raise exception 'superseded forecasts are immutable'"
        )
        self.assertLess(content_guard, null_guard)
        self.assertLess(null_guard, frozen_guard)


class FreeMarketSourcesSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_migration_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(FREE_MARKET_SOURCES_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        self.assertIn("create table if not exists corporate_actions", sql)
        self.assertIn("create table if not exists option_chain_snapshots", sql)
        # Prices distinguish source time from acquisition time in metadata;
        # the column is added to the existing hypertable, never rebuilt.
        self.assertIn(
            "alter table market_data add column if not exists "
            "metadata jsonb not null default '{}'",
            sql,
        )
        # Every trigger creation is guarded so re-applying the file is a no-op.
        self.assertIn("exception when duplicate_object then null", sql)

    def test_corporate_actions_have_point_in_time_identity_and_finite_checks(self):
        sql = self._sql(FREE_MARKET_SOURCES_MIGRATION)
        self.assertIn(
            "create table if not exists corporate_actions ( action_id text primary key,",
            sql,
        )
        self.assertIn("source_timestamp timestamptz not null,", sql)
        self.assertIn("available_at timestamptz not null,", sql)
        self.assertIn(
            "constraint corporate_actions_type_check "
            "check (action_type in ('split', 'dividend'))",
            sql,
        )
        # NaN is rejected by self-equality; infinities by the explicit bound.
        self.assertIn("amount = amount", sql)
        self.assertIn("amount >= 0", sql)
        self.assertIn("amount < 'infinity'::double precision", sql)
        self.assertIn("ratio_numerator > 0", sql)
        self.assertIn("ratio_denominator > 0", sql)
        # Per-type fields: dividends carry amount, splits carry the ratio.
        self.assertIn(
            "constraint corporate_actions_type_fields_check check ( "
            "(action_type = 'dividend' and amount is not null and "
            "ratio_numerator is null and ratio_denominator is null) or "
            "(action_type = 'split' and amount is null and "
            "ratio_numerator is not null and ratio_denominator is not null) )",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_corporate_actions_symbol_effective "
            "on corporate_actions (symbol, effective_date desc)",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_corporate_actions_source_effective "
            "on corporate_actions (source, effective_date desc)",
            sql,
        )

    def test_option_chain_snapshots_match_the_shared_contract(self):
        sql = self._sql(FREE_MARKET_SOURCES_MIGRATION)
        self.assertIn(
            "create table if not exists option_chain_snapshots ( "
            "source text not null, symbol text not null, "
            "contract_symbol text not null, "
            "captured_at timestamptz not null, "
            "source_timestamp timestamptz, expiration date not null, "
            "strike double precision not null, option_type text not null, "
            "bid double precision, ask double precision, last double precision, "
            "volume double precision, open_interest double precision, "
            "implied_volatility double precision, underlying_price double precision, "
            "metadata jsonb not null default '{}', "
            "created_at timestamptz not null default now(), "
            "primary key (source, contract_symbol, captured_at)",
            sql,
        )
        self.assertIn(
            "constraint option_chain_snapshots_option_type_check "
            "check (option_type in ('call', 'put'))",
            sql,
        )
        # Strike is strictly positive and finite; NaN fails strike = strike.
        self.assertIn("strike = strike", sql)
        self.assertIn("strike > 0", sql)
        self.assertIn("implied_volatility <= 10", sql)
        self.assertIn(
            "create index if not exists idx_option_chain_snapshots_symbol_captured "
            "on option_chain_snapshots (symbol, captured_at desc)",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_option_chain_snapshots_expiration "
            "on option_chain_snapshots (expiration)",
            sql,
        )

    def test_immutability_guards_are_installed_after_the_tables(self):
        sql = self._sql(FREE_MARKET_SOURCES_MIGRATION)
        self.assertIn(
            "create or replace function prevent_market_source_mutation()", sql
        )
        self.assertIn("raise exception", sql)
        for table in ("corporate_actions", "option_chain_snapshots"):
            self.assertIn(
                f"create trigger {table}_immutable_guard before update or delete "
                f"on {table} for each row execute function "
                "prevent_market_source_mutation()",
                sql,
            )
        self.assertLess(
            sql.index("create table if not exists corporate_actions"),
            sql.index("create trigger corporate_actions_immutable_guard"),
        )
        self.assertLess(
            sql.index("create table if not exists option_chain_snapshots"),
            sql.index("create trigger option_chain_snapshots_immutable_guard"),
        )


class OptionSnapshotFeaturesSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        uncommented = "\n".join(
            line.split("--", 1)[0] for line in path.read_text().splitlines()
        )
        return " ".join(uncommented.lower().split())

    def test_migration_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(OPTION_SNAPSHOT_FEATURES_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        self.assertIn("create table if not exists option_snapshot_features", sql)
        self.assertIn("create index if not exists", sql)
        # Trigger creation is guarded so re-applying the file is a no-op.
        self.assertIn("exception when duplicate_object then null", sql)

    def test_feature_table_matches_the_shared_contract(self):
        sql = self._sql(OPTION_SNAPSHOT_FEATURES_MIGRATION)
        self.assertIn(
            "create table if not exists option_snapshot_features ( "
            "source text not null, symbol text not null, "
            "captured_at timestamptz not null, "
            "feature_version text not null, "
            "source_timestamp_min timestamptz, source_timestamp_max timestamptz, "
            "available_at timestamptz not null, "
            "contract_count integer not null, "
            "analytics jsonb not null default '{}', "
            "metadata jsonb not null default '{}', "
            "created_at timestamptz not null default now(), "
            "primary key (source, symbol, captured_at)",
            sql,
        )
        # Strict checks: non-empty feature version, non-negative analyzed
        # contract count, object-shaped analytics and metadata.
        self.assertIn(
            "constraint option_snapshot_features_version_check "
            "check (feature_version <> '')",
            sql,
        )
        self.assertIn(
            "constraint option_snapshot_features_contract_count_check "
            "check (contract_count >= 0)",
            sql,
        )
        self.assertIn(
            "constraint option_snapshot_features_analytics_object_check "
            "check (jsonb_typeof(analytics) = 'object')",
            sql,
        )
        self.assertIn(
            "constraint option_snapshot_features_metadata_object_check "
            "check (jsonb_typeof(metadata) = 'object')",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_option_snapshot_features_symbol_captured "
            "on option_snapshot_features (symbol, captured_at desc)",
            sql,
        )

    def test_option_chain_snapshots_become_a_retained_hypertable(self):
        sql = self._sql(OPTION_SNAPSHOT_FEATURES_MIGRATION)
        # Conversion is guarded by an existence check so re-application on
        # an already-chunked table is a no-op; migrate_data moves any
        # pre-conversion rows into chunks.
        self.assertIn("timescaledb_information.hypertables", sql)
        self.assertIn("hypertable_name = 'option_chain_snapshots'", sql)
        self.assertIn(
            "perform create_hypertable( 'option_chain_snapshots', "
            "'captured_at', migrate_data => true )",
            sql,
        )
        # 90-day retention drops raw contract chunks; the long-lived feature
        # table keeps its aggregates and is never chunked or retained.
        self.assertIn(
            "add_retention_policy('option_chain_snapshots', "
            "interval '90 days', if_not_exists => true)",
            sql,
        )
        self.assertNotIn("create_hypertable( 'option_snapshot_features'", sql)
        self.assertNotIn("add_retention_policy('option_snapshot_features'", sql)
        # Conversion happens before the feature table is created.
        self.assertLess(
            sql.index("create_hypertable( 'option_chain_snapshots'"),
            sql.index("create table if not exists option_snapshot_features"),
        )

    def test_immutability_guard_is_installed_after_the_table(self):
        sql = self._sql(OPTION_SNAPSHOT_FEATURES_MIGRATION)
        self.assertIn(
            "create trigger option_snapshot_features_immutable_guard "
            "before update or delete on option_snapshot_features "
            "for each row execute function prevent_market_source_mutation()",
            sql,
        )
        self.assertLess(
            sql.index("create table if not exists option_snapshot_features"),
            sql.index("create trigger option_snapshot_features_immutable_guard"),
        )


class ThesisEventPlaybookSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_migration_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(EVENT_PLAYBOOKS_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        self.assertIn(
            "create table if not exists investment_thesis_event_playbooks", sql
        )
        self.assertIn("create table if not exists investment_thesis_event_matches", sql)
        # Trigger creation is guarded so re-applying the file is a no-op.
        self.assertIn("exception when duplicate_object then null", sql)

    def test_playbooks_are_versioned_immutable_bounded_content(self):
        sql = self._sql(EVENT_PLAYBOOKS_MIGRATION)
        self.assertIn(
            "constraint investment_thesis_event_playbooks_identity_unique "
            "unique (playbook_key, version)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_version_check "
            "check (version >= 1)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_thesis_version_check "
            "check (thesis_version >= 1)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_superseded_after_created "
            "check (superseded_at is null or superseded_at >= created_at)",
            sql,
        )
        # Exactly one active row per playbook key; the rest of history is
        # superseded rows preserved point-in-time.
        self.assertIn(
            "create unique index if not exists "
            "idx_investment_thesis_event_playbooks_active "
            "on investment_thesis_event_playbooks (playbook_key) "
            "where superseded_at is null",
            sql,
        )
        # Bounded content: catalyst length, bounded condition arrays, bounded
        # evidence refs, bounded event types, and a content-addressed
        # fingerprint that cannot mutate.
        self.assertIn(
            "constraint investment_thesis_event_playbooks_catalyst_length_check "
            "check (length(catalyst) between 1 and 2000)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_event_types_bounded_check "
            "check (cardinality(event_types) <= 18)",
            sql,
        )
        for condition in (
            "trigger_conditions",
            "confirmation_conditions",
            "invalidation_conditions",
        ):
            self.assertIn(
                f"constraint investment_thesis_event_playbooks_{condition}_check "
                f"check ( jsonb_typeof({condition}) = 'array' "
                f"and jsonb_array_length({condition}) <= 20 )",
                sql,
            )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_cited_evidence_bounded_check "
            "check (cardinality(cited_evidence_refs) <= 30)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_input_fingerprint_check "
            "check (input_fingerprint ~ '^[0-9a-f]{64}$')",
            sql,
        )
        for leg in ("bull", "base", "bear"):
            self.assertIn(
                f"constraint investment_thesis_event_playbooks_{leg}_scenario_check "
                f"check ({leg}_scenario is null or jsonb_typeof({leg}_scenario) = 'object')",
                sql,
            )

    def test_event_types_are_bounded_to_the_market_event_vocabulary(self):
        sql = self._sql(EVENT_PLAYBOOKS_MIGRATION)
        vocabulary = (
            "price_tick price_bar_closed option_chain_published "
            "corporate_action_published volatility_state_changed "
            "correlation_state_changed macro_release macro_revision "
            "calendar_event_changed headline_published story_updated "
            "regulatory_filing_published transcript_published filing_ingested "
            "central_bank_communication positioning_report_published "
            "source_freshness_changed manual_research_event"
        )
        self.assertIn(
            "constraint investment_thesis_event_playbooks_event_types_vocabulary_check "
            "check (event_types <@ array[",
            sql,
        )
        for event_type in vocabulary.split():
            self.assertIn(f"'{event_type}'", sql)

    def test_match_ledger_is_append_only_with_unique_identity(self):
        sql = self._sql(EVENT_PLAYBOOKS_MIGRATION)
        self.assertIn(
            "constraint investment_thesis_event_matches_match_kind_check "
            "check (match_kind in ('trigger', 'confirmation', 'invalidation', 'context'))",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_matches_identity_unique "
            "unique (playbook_id, market_event_id, match_kind)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_matches_evidence_refs_bounded_check "
            "check (cardinality(evidence_refs) <= 30)",
            sql,
        )
        self.assertIn(
            "constraint investment_thesis_event_matches_assessment_object_check "
            "check (jsonb_typeof(assessment) = 'object')",
            sql,
        )
        self.assertIn(
            "create trigger investment_thesis_event_matches_immutable "
            "before update or delete on investment_thesis_event_matches "
            "for each row execute function reject_event_match_mutation()",
            sql,
        )

    def test_lifecycle_guard_permits_only_the_one_time_supersede_transition(
        self,
    ):
        sql = self._sql(EVENT_PLAYBOOKS_MIGRATION)
        lifecycle = sql[
            sql.index(
                "create or replace function enforce_thesis_event_playbook_lifecycle()"
            ) :
        ]
        # Deletes are rejected outright.
        self.assertIn(
            "if tg_op = 'delete' then raise exception 'event playbooks are append-only'",
            lifecycle,
        )
        # Identity and content (including the row id and fingerprint) stay
        # frozen, so the supersede transition cannot smuggle in any change.
        self.assertIn("new.id is distinct from old.id", lifecycle)
        self.assertIn(
            "new.input_fingerprint is distinct from old.input_fingerprint", lifecycle
        )
        content_guard = lifecycle.index(
            "raise exception 'playbook content is immutable; supersede to revise'"
        )
        # An UPDATE that does not set superseded_at (revision in place) is
        # rejected: the transition must be NULL -> non-NULL.
        null_guard = lifecycle.index(
            "if new.superseded_at is null then "
            "raise exception 'playbook rows are immutable; supersede to revise'"
        )
        # A second transition on an already-superseded row is rejected.
        frozen_guard = lifecycle.index(
            "if old.superseded_at is not null then "
            "raise exception 'superseded playbooks are immutable'"
        )
        self.assertLess(content_guard, null_guard)
        self.assertLess(null_guard, frozen_guard)

    def test_lookup_indexes_are_bounded_and_partial(self):
        sql = self._sql(EVENT_PLAYBOOKS_MIGRATION)
        self.assertIn(
            "create index if not exists idx_investment_thesis_event_playbooks_thesis "
            "on investment_thesis_event_playbooks (thesis_id, created_at desc)",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_investment_thesis_event_playbooks_due "
            "on investment_thesis_event_playbooks (expected_at, created_at) "
            "where superseded_at is null and expected_at is not null",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_investment_thesis_event_playbooks_event_types "
            "on investment_thesis_event_playbooks using gin (event_types) "
            "where superseded_at is null",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_investment_thesis_event_matches_playbook "
            "on investment_thesis_event_matches (playbook_id, created_at desc)",
            sql,
        )
        self.assertIn(
            "create index if not exists idx_investment_thesis_event_matches_event "
            "on investment_thesis_event_matches (market_event_id, created_at desc)",
            sql,
        )


class ForecastScenarioUniquenessSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_migration_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(FORECAST_SCENARIO_UNIQUENESS_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "drop index",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        # The active-scenario invariant is enforced by a partial unique
        # index over non-null scenario ids; scenario-less forecasts stay
        # valid and are outside the index.
        self.assertIn(
            "create unique index if not exists "
            "idx_investment_thesis_forecasts_active_scenario "
            "on investment_thesis_forecasts (scenario_id) "
            "where scenario_id is not null and superseded_at is null",
            sql,
        )

    def test_legacy_duplicates_are_deterministically_superseded_before_the_index(
        self,
    ):
        sql = self._sql(FORECAST_SCENARIO_UNIQUENESS_MIGRATION)
        dedupe = sql.index("update investment_thesis_forecasts")
        index = sql.index(
            "create unique index if not exists "
            "idx_investment_thesis_forecasts_active_scenario"
        )
        self.assertLess(dedupe, index)
        # The earliest frozen row per scenario wins (created_at, then id).
        self.assertIn(
            "row_number() over ( partition by scenario_id order by created_at, id )",
            sql,
        )
        # Superseding at the row's own created_at satisfies the
        # superseded_after_created CHECK and the lifecycle trigger's
        # one-time NULL -> non-NULL transition; duplicates are never
        # deleted, only frozen.
        self.assertIn("superseded_at = f.created_at", sql)
        self.assertIn("where scenario_id is not null", sql)
        self.assertIn("and superseded_at is null", sql)
        self.assertIn("kept_rank > 1", sql)


class CatalystImmutabilitySchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        return " ".join(path.read_text().lower().split())

    def test_migration_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(CATALYST_IMMUTABILITY_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "drop index",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        # Re-application is a no-op: the function is CREATE OR REPLACE and
        # the trigger is dropped then recreated.
        self.assertIn(
            "create or replace function enforce_investment_catalyst_immutability()",
            sql,
        )
        self.assertIn(
            "drop trigger if exists investment_catalysts_immutable "
            "on investment_catalysts",
            sql,
        )
        self.assertIn(
            "create trigger investment_catalysts_immutable "
            "before update or delete on investment_catalysts",
            sql,
        )

    def test_legacy_rows_are_stamped_conservatively_before_the_trigger(self):
        sql = self._sql(CATALYST_IMMUTABILITY_MIGRATION)
        # The legacy stamp runs before the guard exists, exactly once: it is
        # guarded by the trigger's absence, so re-application (and any row
        # inserted after the migration, whose updated_at equals created_at)
        # is never re-stamped.
        stamp = sql.index(
            "update investment_catalysts "
            "set updated_at = greatest(coalesce(updated_at, created_at), now())"
        )
        trigger = sql.index("create trigger investment_catalysts_immutable")
        self.assertLess(stamp, trigger)
        self.assertIn("pg_trigger", sql)
        self.assertIn("tgname = 'investment_catalysts_immutable'", sql)
        self.assertIn("tgrelid = 'investment_catalysts'::regclass", sql)
        # Every legacy row is stamped, with no row filter: mutation history
        # is unknowable for all of them, so even a row whose updated_at is
        # already past created_at is only valid from the migration on.
        self.assertNotIn("where", sql[stamp:trigger])
        # The stamp is conservative: GREATEST/COALESCE never move a
        # timestamp backward (an already-later updated_at is kept).
        self.assertIn("greatest(coalesce(updated_at, created_at), now())", sql)

    def test_replay_input_mutations_are_rejected_including_updated_at(self):
        sql = self._sql(CATALYST_IMMUTABILITY_MIGRATION)
        lifecycle = sql[
            sql.index(
                "create or replace function enforce_investment_catalyst_immutability()"
            ) : sql.index(
                "drop trigger if exists investment_catalysts_immutable "
                "on investment_catalysts"
            )
        ]
        # Deletes are append-only; every replay input is frozen: the
        # scoring/identity fields and the updated_at visibility gate alike.
        self.assertIn("tg_op = 'delete'", lifecycle)
        self.assertIn("raise exception 'catalysts are append-only'", lifecycle)
        for column in (
            "new.id is distinct from old.id",
            "new.thesis_id is distinct from old.thesis_id",
            "new.description is distinct from old.description",
            "new.expected_at is distinct from old.expected_at",
            "new.state is distinct from old.state",
            "new.created_at is distinct from old.created_at",
            "new.updated_at is distinct from old.updated_at",
        ):
            self.assertIn(column, lifecycle)
        self.assertIn(
            "raise exception 'catalyst replay inputs are immutable after insert'",
            lifecycle,
        )


class ThesisFusionReferenceSchemaTests(unittest.TestCase):
    @staticmethod
    def _sql(path: Path) -> str:
        uncommented = "\n".join(
            line.split("--", 1)[0] for line in path.read_text().splitlines()
        )
        return " ".join(uncommented.lower().split())

    def test_migration_is_additive_idempotent_and_never_destroys_data(self):
        sql = self._sql(THESIS_FUSION_REFERENCE_MIGRATION)
        for destructive_statement in (
            "drop table",
            "drop column",
            "drop index",
            "delete from",
            "truncate",
        ):
            self.assertNotIn(destructive_statement, sql)
        self.assertIn(
            "alter table investment_theses "
            "add column if not exists fusion_reference_at timestamptz",
            sql,
        )
        # The guard pair: the accepted candidate fingerprint is persisted
        # alongside the reference, additively and idempotently.
        self.assertIn(
            "alter table investment_theses "
            "add column if not exists fusion_candidate_fingerprint text",
            sql,
        )
        # No index: the guard pair is only read through the primary-key
        # thesis lookup inside the merge claim (point reads).
        self.assertNotIn("create index", sql)

    def test_legacy_rows_are_backfilled_conservatively(self):
        sql = self._sql(THESIS_FUSION_REFERENCE_MIGRATION)
        # The conservative legacy stamp is the greatest of every known
        # accepted/current timestamp, never a preference chain: a content
        # update or evaluation bumps updated_at/last_evaluated_at, and any
        # lesser choice would admit a replay between the two to overwrite
        # current legacy content.
        self.assertIn(
            "set fusion_reference_at = greatest( "
            "created_at, updated_at, coalesce(last_evaluated_at, created_at) )",
            sql,
        )
        # COALESCE covers the one nullable column; the NOT NULL created_at
        # and updated_at floor every stamp, so no row ends up NULL.
        self.assertIn("coalesce(last_evaluated_at, created_at)", sql)
        self.assertIn("created_at, updated_at", sql)
        # The backfill only stamps rows without a guard, so re-applying the
        # file (and fresh installs) never rewrite an existing reference.
        self.assertIn("where fusion_reference_at is null", sql)
        # The column exists before the backfill touches it.
        self.assertLess(
            sql.index("add column if not exists fusion_reference_at"),
            sql.index("update investment_theses"),
        )

    def test_fingerprint_column_is_nullable_and_never_backfilled(self):
        sql = self._sql(THESIS_FUSION_REFERENCE_MIGRATION)
        # The fingerprint is nullable: manual rows and pre-migration rows
        # carry no proven candidate.  There is deliberately NO backfill --
        # the candidate that produced legacy content is unknowable, and the
        # only honest value is NULL, which makes equal-reference claims
        # against it fail closed (unprovable).
        self.assertIn("add column if not exists fusion_candidate_fingerprint text", sql)
        self.assertNotIn("set fusion_candidate_fingerprint", sql)
        # The reference backfill never fabricates a fingerprint, and no
        # UPDATE statement mentions the fingerprint column at all.
        for statement in sql.split(";"):
            if statement.lstrip().startswith("update investment_theses"):
                self.assertNotIn("fusion_candidate_fingerprint", statement)


if __name__ == "__main__":
    unittest.main()
