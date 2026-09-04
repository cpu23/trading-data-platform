import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["DEPLOYMENT_MODE"] = "test"

from auth import mint_csrf_token

MOCK_CONFIG = {
    "logging": {"level": "INFO"},
    "database": {
        "host": "localhost",
        "name": "test",
        "user": "test",
        "password": "test",
    },
    "dashboard": {
        "indicators": [],
        "stale_thresholds": {
            "briefing_hours": 18,
            "regime_hours": 18,
            "macro_hours": 30,
            "events_hours": 8,
        },
    },
    "collectors": {},
    "processors": {},
    "budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80},
}
CSRF_TOKEN = mint_csrf_token()
AUTH = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Origin": "http://testserver",
    "X-CSRF-Token": CSRF_TOKEN,
}

with patch("config.load_config", return_value=MOCK_CONFIG):
    from main import create_app
from fastapi.testclient import TestClient


class ApiLifespanTests(unittest.TestCase):
    @patch("main.load_config", return_value=MOCK_CONFIG)
    @patch("main.setup_logging")
    def test_app_state_has_no_orchestrator_client(
        self,
        _setup_logging,
        _load_config,
    ):
        app = create_app()
        with TestClient(app) as _client:
            self.assertFalse(hasattr(app.state, "orchestrator_client"))


if __name__ == "__main__":
    unittest.main()
