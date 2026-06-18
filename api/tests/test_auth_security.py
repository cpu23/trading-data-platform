import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auth


class AuthSecurityTests(unittest.TestCase):
    def test_password_hash_round_trip(self):
        record = auth.hash_password("a sufficiently long password")
        self.assertTrue(auth.verify_password("a sufficiently long password", record))
        self.assertFalse(auth.verify_password("wrong password", record))

    def test_admin_file_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            with patch.object(auth, "STATE_DIR", Path(directory)), patch.object(auth, "AUTH_FILE", path):
                auth.create_admin("a sufficiently long password")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
