import unittest
from pathlib import Path


class CleanMigrationScriptTests(unittest.TestCase):
    def test_clean_migration_gate_is_bounded_idempotent_and_always_cleans_up(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "test_clean_migrations.sh").read_text()
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("down --volumes --remove-orphans", script)
        self.assertIn("MAX_ATTEMPTS", script)
        self.assertGreaterEqual(script.count("run --rm migrate"), 2)
        self.assertIn("no pending", script.lower())


class CleanMigrationFailureGuardTests(unittest.TestCase):
    def test_clean_migration_script_fails_fast_and_guards_every_failure_path(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "test_clean_migrations.sh").read_text()
        self.assertIn("set -euo pipefail", script)
        for message in (
            "FAIL postgres did not become healthy",
            "FAIL initial migration run",
            "FAIL migration rerun was not idempotent",
        ):
            self.assertIn(message, script)
            line = next(line for line in script.splitlines() if message in line)
            self.assertIn("exit 1", line)

    def test_idempotency_rerun_hits_the_same_database(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "test_clean_migrations.sh").read_text()
        # The rerun must observe the schema produced by the first run: a
        # volume teardown between runs would make "no pending" vacuous and
        # would never catch a migration (e.g. 049's guarded constraint
        # swap) that breaks re-application.
        first_run = script.index("run --rm migrate")
        second_run = script.index("run --rm migrate", first_run + 1)
        self.assertNotIn("down", script[first_run:second_run])

    def test_clean_migration_run_lands_on_the_event_playbook_migration(self):
        root = Path(__file__).resolve().parents[1]
        migrations = sorted(
            (root / "db" / "migrations").glob("*.sql"),
            key=lambda path: int(path.name.split("_", 1)[0]),
        )
        versions = [int(path.name.split("_", 1)[0]) for path in migrations]
        # The clean run applies every pending migration, and the second run
        # is the idempotency gate that would fail if 051 were not
        # re-appliable, so the run must land on or past 051.
        self.assertIn(51, versions)
        self.assertGreaterEqual(versions[-1], 51)
        script = (root / "scripts" / "test_clean_migrations.sh").read_text()
        self.assertGreaterEqual(script.count("run --rm migrate"), 2)
        self.assertIn("no pending", script.lower())


if __name__ == "__main__":
    unittest.main()
