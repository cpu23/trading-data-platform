import unittest
from pathlib import Path


class ServiceContractScriptTests(unittest.TestCase):
    def test_live_contract_runner_covers_required_boundaries(self):
        source = (Path(__file__).resolve().parents[1] / "scripts/test_service_contracts.py").read_text()
        for marker in (
            "/api/system/health", "/quality", "/api/news/sources", "/api/news/feed",
            "/api/triggers/collectors/not-real", "401", "404", "abandoned",
            "restart", "ON_ERROR_STOP",
        ):
            self.assertIn(marker, source)

    def test_demo_smoke_invokes_live_contract_runner_in_isolated_project(self):
        source = (Path(__file__).resolve().parents[1] / "scripts/smoke_test.sh").read_text()
        self.assertIn("scripts/test_service_contracts.py", source)
        self.assertIn("COMPOSE_PROJECT_NAME", source)
        self.assertIn("trading-data-platform-demo-smoke", source)


if __name__ == "__main__":
    unittest.main()
