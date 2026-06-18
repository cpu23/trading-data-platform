import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import _consume_budget_override, run_processor


class BudgetEnforcementTests(unittest.TestCase):
    @patch("orchestrator.get_session")
    def test_override_consumption_is_recorded_in_existing_run_summary(
        self,
        get_session,
    ):
        session = Mock()
        row = SimpleNamespace(
            _mapping={
                "summary": {
                    "budget_override": {
                        "requested": True,
                        "reason": "manual review",
                    }
                }
            }
        )
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value.__enter__.return_value = session

        override = _consume_budget_override("run-id", {})

        self.assertEqual(override["consumed_by"], "run-id")
        update_params = session.execute.call_args_list[1].args[1]
        self.assertIn('"consumed_at"', update_params["summary"])
        self.assertIn('"consumed_by": "run-id"', update_params["summary"])

    @patch("orchestrator.get_session")
    def test_consumed_cycle_override_remains_valid_for_same_run(self, get_session):
        override = {
            "requested": True,
            "reason": "manual review",
            "consumed_at": "2026-06-18T12:00:00+00:00",
            "consumed_by": "run-id",
        }
        session = Mock()
        session.execute.return_value.fetchone.return_value = SimpleNamespace(
            _mapping={"summary": {"budget_override": override}}
        )
        get_session.return_value.__enter__.return_value = session

        result = _consume_budget_override("run-id", {})

        self.assertEqual(result, override)
        self.assertEqual(session.execute.call_count, 1)

    @patch("orchestrator.finish_run")
    @patch("orchestrator._write_processing_log")
    @patch("orchestrator._consume_budget_override", return_value=None)
    @patch("orchestrator._get_budget_status")
    @patch("orchestrator.get_processor")
    @patch("orchestrator.ensure_run")
    def test_denied_processor_does_not_process_or_publish(
        self,
        ensure_run,
        get_processor,
        budget_status,
        consume_override,
        write_log,
        finish_run,
    ):
        processor = Mock()
        get_processor.return_value = processor
        budget_status.return_value = {
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }

        result = run_processor("briefing", config={})

        self.assertEqual(result["status"], "budget_denied")
        get_processor.assert_not_called()
        processor.process.assert_not_called()
        write_log.assert_called_once()
        finish_run.assert_called_once()

    @patch("orchestrator.finish_run")
    @patch("orchestrator._write_processing_log")
    @patch("orchestrator._persist_processor_result")
    @patch("orchestrator._consume_budget_override")
    @patch("orchestrator._get_budget_status")
    @patch("orchestrator.get_processor")
    @patch("orchestrator.ensure_run")
    def test_one_run_override_allows_processor_and_is_audited(
        self,
        ensure_run,
        get_processor,
        budget_status,
        consume_override,
        persist_result,
        write_log,
        finish_run,
    ):
        override = {
            "requested": True,
            "reason": "manual review",
            "consumed_at": "2026-06-18T12:00:00+00:00",
        }
        consume_override.return_value = override
        budget_status.return_value = {
            "paid_calls_allowed": False,
            "today_cost_usd": 2.0,
            "budget_cap_usd": 2.0,
        }
        processor = Mock()
        processor.process.return_value = {
            "opinion": {"opinion_id": "opinion-1"},
            "processing_log": {},
        }
        get_processor.return_value = processor

        result = run_processor("briefing", config={})

        self.assertEqual(result["status"], "success")
        processor.process.assert_called_once()
        self.assertEqual(result["budget_override"], override)
        logged_input = persist_result.call_args.kwargs["processing_log"]["input_summary"]
        self.assertEqual(logged_input["budget_override"], override)


if __name__ == "__main__":
    unittest.main()
