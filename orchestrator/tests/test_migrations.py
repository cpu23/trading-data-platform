import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import migrate


class MigrationInventoryTests(unittest.TestCase):
    def test_missing_migrations_directory_is_fatal(self):
        with tempfile.TemporaryDirectory() as parent:
            missing = Path(parent) / "missing"
            with (
                patch.object(migrate, "MIGRATIONS_DIR", str(missing)),
                patch.object(migrate, "ensure_tracking_table"),
                patch.object(migrate, "get_applied_versions", return_value=set()),
            ):
                with self.assertRaisesRegex(FileNotFoundError, str(missing)):
                    migrate.run_migrations({})


if __name__ == "__main__":
    unittest.main()
