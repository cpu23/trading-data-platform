import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import _prepare_record, insert_records_in_session
from orchestrator import _run_processor_impl


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

        result = _run_processor_impl(
            "asset_panel",
            config={},
            correlation_id="cycle-id",
            manage_lifecycle=False,
        )

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
            / "015_versioned_intelligence.sql"
        ).read_text()
        self.assertIn("lifecycle_status", migration)
        self.assertIn("publication_status", migration)
        self.assertIn("output_ids", migration)
        self.assertIn("retention_days INTEGER DEFAULT 90", migration)

    def test_processing_output_ids_remain_native_array(self):
        output_ids = ["26a71a75-52b5-40bb-96a1-e8f08d3249e6"]
        prepared = _prepare_record(
            {"output_ids": output_ids, "input_summary": {"ok": True}},
            "processing_log",
        )
        self.assertEqual(prepared["output_ids"], output_ids)
        self.assertEqual(prepared["input_summary"], '{"ok": true}')

    def test_processing_log_insert_casts_output_ids_to_uuid_array(self):
        session = Mock()
        insert_records_in_session(
            session,
            "processing_log",
            [
                {
                    "processor": "macro_regime",
                    "output_ids": ["26a71a75-52b5-40bb-96a1-e8f08d3249e6"],
                }
            ],
        )
        statement = str(session.execute.call_args.args[0])
        self.assertIn("CAST(:output_ids AS UUID[])", statement)


if __name__ == "__main__":
    unittest.main()
