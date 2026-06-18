import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import run_processor


class VersionedIntelligenceTests(unittest.TestCase):
    @patch("orchestrator._get_budget_status")
    @patch("orchestrator._consume_budget_override", return_value=None)
    @patch("orchestrator.ensure_run")
    @patch("orchestrator.get_processor")
    @patch("orchestrator._persist_processor_result")
    def test_multi_opinion_result_is_versioned_and_staged_atomically(
        self,
        persist,
        get_processor,
        ensure_run,
        consume_override,
        budget,
    ):
        budget.return_value = {"paid_calls_allowed": True}
        processor = Mock()
        processor.process.return_value = {
            "opinions": [
                {
                    "opinion_id": "one",
                    "opinion_type": "asset_panel",
                    "scope": "asset:EURUSD",
                },
                {
                    "opinion_id": "two",
                    "opinion_type": "asset_panel",
                    "scope": "asset:XAUUSD",
                },
            ],
            "processing_log": {},
        }
        get_processor.return_value = processor

        result = run_processor("asset_panel", config={}, correlation_id="cycle-id")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["opinion_ids"], ["one", "two"])
        opinions = persist.call_args.kwargs["opinions"]
        self.assertEqual({item["correlation_id"] for item in opinions}, {"cycle-id"})
        self.assertEqual({item["lifecycle_status"] for item in opinions}, {"validated"})
        self.assertEqual({item["schema_version"] for item in opinions}, {"1"})

    def test_migration_defines_publication_and_retention_contract(self):
        migration = (
            Path(__file__).resolve().parents[2]
            / "db"
            / "migrations"
            / "008_versioned_intelligence.sql"
        ).read_text()
        self.assertIn("lifecycle_status", migration)
        self.assertIn("publication_status", migration)
        self.assertIn("output_ids", migration)
        self.assertIn("retention_days INTEGER DEFAULT 90", migration)


if __name__ == "__main__":
    unittest.main()
