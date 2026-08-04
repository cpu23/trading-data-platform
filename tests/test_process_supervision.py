import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProcessSupervisionTests(unittest.TestCase):
    def test_no_legacy_multi_process_entrypoint_remains(self):
        self.assertFalse((ROOT / "orchestrator" / "entrypoint.sh").exists())
        self.assertFalse((ROOT / "orchestrator" / "Dockerfile").exists())
        self.assertFalse((ROOT / "api" / "Dockerfile").exists())
        for script in ROOT.rglob("*.sh"):
            source = script.read_text()
            bare_background = re.findall(r"(?m)^[^#\n]*\s&\s*(?:$|#)", source)
            self.assertEqual(bare_background, [], f"bare background process in {script}")

    def test_smoke_has_bounded_health_fixture_and_migration_checks(self):
        source = (ROOT / "scripts" / "smoke_test.sh").read_text()
        self.assertIn("MAX_ATTEMPTS=", source)
        self.assertIn("service_completed_successfully", (ROOT / "docs" / "adr" / "001-runtime-topology.md").read_text())
        self.assertIn("docker compose -f \"$COMPOSE_FILE\" ps --status exited migrate", source)
        self.assertIn("/api/system/health", source)
        self.assertIn("/health", source)
        self.assertIn("demo/deterministic", source)
        self.assertIn("controlled_expansion", source)

    def test_smoke_kills_each_service_and_asserts_restart_recovery(self):
        source = (ROOT / "scripts" / "smoke_test.sh").read_text()
        for service in ("api", "orchestrator"):
            self.assertIn(f'assert_restart "{service}"', source)
        self.assertIn(".State.StartedAt", source)
        self.assertIn("wait_for_healthy", source)
        self.assertIn("assert_api_unavailable", source)
        self.assertIn('compose stop -t 0 "$service"', source)
        self.assertIn('compose start "$service"', source)

    def test_smoke_always_tears_down_volumes_and_orphans(self):
        source = (ROOT / "scripts" / "smoke_test.sh").read_text()
        self.assertIn("trap cleanup EXIT", source)
        self.assertIn("down --volumes --remove-orphans", source)


if __name__ == "__main__":
    unittest.main()
