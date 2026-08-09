import unittest
from unittest.mock import patch

from click.testing import CliRunner

from cli import cli


class CollectorCliTests(unittest.TestCase):
    def test_collect_all_renders_scheduled_skips_without_crashing(self):
        cycle_result = {
            "collectors": {
                "fred": {
                    "status": "success",
                    "records_fetched": 3,
                    "records_written": 3,
                    "duration_ms": 12,
                },
                "cftc": {
                    "status": "skipped",
                    "reason": "not_due",
                    "mode": "refresh",
                },
            },
            "processors": {},
            "status": "success",
        }
        with (
            patch("cli.load_config", return_value={}),
            patch("cli.setup_logging"),
            patch("cli.run_full_cycle", return_value=cycle_result),
        ):
            result = CliRunner().invoke(cli, ["collect", "--all"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("fred: success (3 fetched, 3 written, 12ms)", result.output)
        self.assertIn("cftc: skipped (not_due)", result.output)
        self.assertIn("Overall: success", result.output)


if __name__ == "__main__":
    unittest.main()
