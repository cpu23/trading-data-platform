import subprocess
import sys
import unittest
from pathlib import Path


class FailureDrillScriptTests(unittest.TestCase):
    def test_failure_drills_unit_only_is_deterministic_and_successful(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "scripts/failure_drills.py", "--unit-only"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        expected = {
            "API DB unavailable",
            "worker heartbeat unavailable",
            "malformed config fail closed",
            "collector truthful partial/failed",
            "LLM timeout safe telemetry",
            "partial DB write retains spend",
            "restart prerequisite",
            "concurrent cycle conflict",
            "news cursor unchanged",
        }
        actual = {
            line[5:] for line in result.stdout.splitlines() if line.startswith("PASS ")
        }
        self.assertEqual(actual, expected)
        self.assertNotIn("FAIL", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_failure_drills_docker_is_opt_in_and_invokes_existing_smoke(self):
        script = Path(__file__).resolve().parents[1] / "scripts/failure_drills.py"
        source = script.read_text()
        self.assertIn("--docker", source)
        self.assertIn("smoke_test.sh", source)
        self.assertIn("--unit-only", source)


if __name__ == "__main__":
    unittest.main()
