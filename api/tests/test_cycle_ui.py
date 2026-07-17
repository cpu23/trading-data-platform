import re
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]


class CycleModeHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.header = (API_ROOT / "templates/partials/header.html").read_text()
        cls.app_js = (API_ROOT / "static/app.js").read_text()

    def test_header_has_one_clear_due_cycle_action_and_explicit_mode_menu(self):
        self.assertEqual(self.header.count('id="run-cycle-btn"'), 1)
        self.assertIn('data-cycle-mode="refresh"', self.header)
        self.assertRegex(
            self.header,
            r'id="run-cycle-btn"[\s\S]*?<span class="btn-label">Run due cycle</span>',
        )
        self.assertIn('id="cycle-mode-select"', self.header)
        self.assertIn('aria-label="Choose cycle mode"', self.header)
        self.assertIn('<option value="analyze">Analyze stored data</option>', self.header)
        self.assertIn('<option value="force_full">Rebuild everything (uses budget)</option>', self.header)
        self.assertNotIn('>More…<', self.header)
        self.assertNotIn('data-cycle-mode="analyze" class="btn"', self.header)
        self.assertNotIn('data-cycle-mode="force_full" class="btn"', self.header)

    def test_javascript_uses_one_json_trigger_path_with_force_confirmation(self):
        self.assertIn('function triggerCycle(mode, budgetConfirmed)', self.app_js)
        self.assertIn("JSON.stringify({mode: mode, budget_confirmed: budgetConfirmed})", self.app_js)
        self.assertIn("triggerCycle('refresh', false)", self.app_js)
        self.assertRegex(
            self.app_js,
            re.compile(r"daily budget bypass", re.IGNORECASE),
        )
        self.assertIn("if (!window.confirm", self.app_js)
        self.assertIn("triggerCycle(mode, mode === 'force_full')", self.app_js)

    def test_javascript_handles_documented_cycle_response_statuses(self):
        for status in (202, 409, 422, 503):
            with self.subTest(status=status):
                self.assertIn(str(status), self.app_js)


if __name__ == "__main__":
    unittest.main()
