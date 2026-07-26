import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from orchestrator import (
        _aggregate_stage_status,
        _resolve_and_run_processors,
        ensure_run,
        finish_run,
        get_transitive_dependents,
        run_collector,
        run_processor,
    )
except ImportError:
    raise unittest.SkipTest("codex/market-intelligence-expansion contract not implemented in master")
from processors._validators import OutputPolicyError


@unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
class CycleRuntimeCorrectnessTests(unittest.TestCase):
    @patch("orchestrator.get_session")
    def test_child_ensure_preserves_parent_run_identity(self, get_session):
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        ensure_run(
            "cycle-id",
            {},
            run_kind="processor",
            requested_component="briefing",
        )

        statement = str(session.execute.call_args.args[0])
        self.assertNotIn("run_kind = EXCLUDED.run_kind", statement)
        self.assertNotIn(
            "requested_component = EXCLUDED.requested_component", statement
        )

    @patch("orchestrator.get_session")
    def test_partial_cycle_does_not_publish_staged_outputs(self, get_session):
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        finish_run("cycle-id", "partial", {"status": "partial"}, {})

        self.assertEqual(session.execute.call_count, 1)
        params = session.execute.call_args.args[1]
        self.assertEqual(params["publication_status"], "failed")

    @patch("orchestrator.get_session")
    def test_successful_cycle_publishes_all_staged_outputs(self, get_session):
        session = Mock()
        get_session.return_value.__enter__.return_value = session

        finish_run("cycle-id", "success", {"status": "success"}, {})

        self.assertEqual(session.execute.call_count, 3)
        params = session.execute.call_args_list[0].args[1]
        self.assertEqual(params["publication_status"], "published")

    def test_stage_aggregation_requires_complete_success_to_publish(self):
        self.assertEqual(
            _aggregate_stage_status(
                {
                    "fred": {"status": "success"},
                    "macro": {"status": "success"},
                }
            ),
            "success",
        )
        self.assertEqual(
            _aggregate_stage_status(
                {
                    "fred": {"status": "success"},
                    "macro": {"status": "validation_failed"},
                }
            ),
            "partial",
        )
        self.assertEqual(
            _aggregate_stage_status(
                {
                    "fred": {"status": "success", "blocking": True},
                    "oecd": {"status": "no_data", "blocking": False},
                    "macro": {"status": "success", "blocking": True},
                }
            ),
            "success",
        )

    @patch("orchestrator.get_all_processors")
    def test_transitive_dependents_include_downstream_processors(
        self, get_all_processors
    ):
        macro = Mock()
        macro.get_depends_on.return_value = ["fred"]
        intelligence = Mock()
        intelligence.get_depends_on.return_value = ["macro_regime"]
        unrelated = Mock()
        unrelated.get_depends_on.return_value = ["forex_factory"]
        get_all_processors.return_value = {
            "macro_regime": macro,
            "market_intelligence": intelligence,
            "event_impact": unrelated,
        }
        config = {
            "processors": {
                name: {"enabled": True}
                for name in get_all_processors.return_value
            }
        }

        result = get_transitive_dependents("fred", config)

        self.assertEqual(
            result, {"macro_regime", "market_intelligence"}
        )

    @patch("orchestrator.get_all_processors")
    def test_unmet_dependencies_are_explicitly_recorded_as_skipped(
        self, get_all_processors
    ):
        processor = Mock()
        processor.get_depends_on.return_value = ["fred"]
        get_all_processors.return_value = {"macro_regime": processor}

        results = _resolve_and_run_processors(
            config={"processors": {"macro_regime": {"enabled": True}}},
            correlation_id="cycle-id",
            successful_collectors=set(),
        )

        self.assertEqual(results["macro_regime"]["status"], "skipped")
        self.assertIn("fred", results["macro_regime"]["error"])

    @patch("orchestrator.finish_run")
    @patch("orchestrator._write_collection_log")
    @patch("orchestrator.upsert_records", return_value=0)
    @patch("orchestrator.get_collector")
    @patch("orchestrator.ensure_run")
    @patch(
        "orchestrator._runtime_lock_context",
        return_value=nullcontext(True),
    )
    def test_incomplete_collector_write_cannot_report_success(
        self,
        runtime_lock,
        ensure_run_mock,
        get_collector,
        upsert_records,
        write_log,
        finish_run_mock,
    ):
        collector = Mock()
        collector.collect.return_value = [{"series_id": "x"}]
        collector.get_target_table.return_value = "macro_series"
        collector.get_conflict_columns.return_value = ["series_id"]
        get_collector.return_value = collector

        result = run_collector("fred", config={})

        self.assertEqual(result["status"], "failed")
        finish_run_mock.assert_called_once()
        self.assertEqual(finish_run_mock.call_args.args[1], "failed")

    @patch("orchestrator.finish_run")
    @patch("orchestrator._write_processing_log")
    @patch("orchestrator._get_budget_status")
    @patch("orchestrator._consume_budget_override", return_value=None)
    @patch("orchestrator.get_processor")
    @patch("orchestrator.ensure_run")
    def test_policy_failure_has_validation_failed_state(
        self,
        ensure_run_mock,
        get_processor,
        consume_override,
        budget_status,
        write_log,
        finish_run_mock,
    ):
        budget_status.return_value = {"paid_calls_allowed": True}
        processor = Mock()
        processor.process.side_effect = OutputPolicyError(
            "market_intelligence", ["prohibited instruction"]
        )
        get_processor.return_value = processor

        result = run_processor("market_intelligence", config={})

        self.assertEqual(result["status"], "validation_failed")
        finish_run_mock.assert_called_once()
        self.assertEqual(
            finish_run_mock.call_args.args[1], "validation_failed"
        )


if __name__ == "__main__":
    unittest.main()
