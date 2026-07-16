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


if __name__ == "__main__":
    unittest.main()
