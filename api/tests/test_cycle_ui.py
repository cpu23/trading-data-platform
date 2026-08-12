import re
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]


class CycleModeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = (API_ROOT / "templates/settings.html").read_text()
        cls.app_js = (API_ROOT / "static/app.js").read_text()

    def test_settings_has_cycle_controls_with_explicit_modes(self):
        self.assertEqual(self.settings.count('id="run-cycle-btn"'), 1)
        self.assertEqual(self.settings.count('id="force-cycle-btn"'), 1)
        self.assertIn('data-mode="refresh"', self.settings)
        self.assertIn('data-mode="force_full"', self.settings)
        self.assertIn("Run due cycle", self.settings)
        self.assertIn("Force full cycle", self.settings)
        self.assertIn('id="cycle-progress"', self.settings)
        self.assertIn('id="cycle-progress-text"', self.settings)
        self.assertIn('id="cycle-progress-fill"', self.settings)
        self.assertIn('id="cycle-result"', self.settings)

    def test_javascript_uses_one_json_trigger_path_with_force_confirmation(self):
        self.assertIn("function triggerCycle(mode)", self.app_js)
        self.assertIn("budget_confirmed: mode === 'force_full'", self.app_js)
        self.assertIn("triggerCycle(mode)", self.app_js)
        self.assertRegex(
            self.app_js,
            re.compile(r"uses more budget", re.IGNORECASE),
        )
        self.assertIn("if (!window.confirm", self.app_js)

    def test_javascript_handles_documented_cycle_response_statuses(self):
        for status in (202, 409, 422, 503):
            with self.subTest(status=status):
                self.assertIn(str(status), self.app_js)

    def test_javascript_stops_for_every_durable_terminal_result(self):
        for status in (
            "success",
            "partial",
            "failed",
            "validation_failed",
            "budget_denied",
            "budget_blocked",
            "budget_unavailable",
        ):
            with self.subTest(status=status):
                self.assertIn(f"'{status}'", self.app_js)


if __name__ == "__main__":
    unittest.main()
