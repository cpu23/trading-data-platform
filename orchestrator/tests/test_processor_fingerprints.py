import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from budgets import BudgetContext
from processors.base import canonical_fingerprint, canonical_json_value
from processors.briefing import DailyBriefingProcessor
from processors.macro_regime import MacroRegimeProcessor

import orchestrator
from contracts.runtime_config import ProcessorAssetContextConfig


class Row:
    def __init__(self, **values):
        self._mapping = values


class FingerprintedProcessor:
    processor_id = "macro_regime"
    PROCESSOR_SCHEMA_VERSION = "macro-schema-1"

    def __init__(self, inputs=None):
        self.inputs = inputs or {"series": [{"series_id": "GDP", "value": 1.0}]}
        self.process = Mock(
            return_value={
                "opinion": {"opinion_id": "11111111-1111-1111-1111-111111111111"},
                "extra_records": {},
                "processing_log": {
                    "status": "success",
                    "tokens_input": 2,
                    "tokens_output": 3,
                    "cost_usd": 0.1,
                },
            }
        )

    def get_prompt_version(self):
        return "prompt-v1"

    def get_prompt_identity(self, config):
        return config.get(
            "prompt_identity",
            {"path": "prompts/test.txt", "sha256": "a" * 64},
        )

    def get_fingerprint_inputs(self, config):
        return self.inputs

    def get_depends_on(self):
        return ["fred"]


class CanonicalFingerprintTests(unittest.TestCase):
    def test_canonical_json_is_order_independent_and_normalizes_utc(self):
        first = {
            "b": 2,
            "a": datetime(2026, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=2))),
        }
        second = {"a": datetime(2026, 1, 2, 1, 4, tzinfo=UTC), "b": 2}
        self.assertEqual(canonical_fingerprint(first), canonical_fingerprint(second))
        self.assertRegex(canonical_fingerprint(first), r"^[0-9a-f]{64}$")

    def test_uuid_values_are_canonicalized_as_strings(self):
        value = UUID("11111111-1111-4111-8111-111111111111")
        self.assertEqual(
            canonical_fingerprint({"opinion_id": value}),
            canonical_fingerprint({"opinion_id": str(value)}),
        )

    def test_runtime_config_mappings_are_json_compatible(self):
        configured = ProcessorAssetContextConfig(
            channels=["relative monetary policy"],
            positioning_effects={"EUR": "positive"},
        )
        expected = {
            "channels": ["relative monetary policy"],
            "positioning_effects": {"EUR": "positive"},
            "channel_effects": [],
        }

        self.assertEqual(canonical_json_value(configured), expected)
        self.assertEqual(
            canonical_fingerprint({"asset": configured}),
            canonical_fingerprint({"asset": expected}),
        )

    def test_prompt_model_schema_and_input_changes_change_fingerprint(self):
        processor = FingerprintedProcessor()
        base = {"llm": {"models": {"default": "provider/a"}}}
        fingerprint = orchestrator.build_processor_fingerprint(processor, base)

        processor.get_prompt_version = Mock(return_value="prompt-v2")
        self.assertNotEqual(
            fingerprint, orchestrator.build_processor_fingerprint(processor, base)
        )
        processor.get_prompt_version = Mock(return_value="prompt-v1")
        self.assertNotEqual(
            fingerprint,
            orchestrator.build_processor_fingerprint(
                processor, {"llm": {"models": {"default": "provider/b"}}}
            ),
        )
        processor.PROCESSOR_SCHEMA_VERSION = "macro-schema-2"
        self.assertNotEqual(
            fingerprint, orchestrator.build_processor_fingerprint(processor, base)
        )
        processor.PROCESSOR_SCHEMA_VERSION = "macro-schema-1"
        processor.inputs = {"series": [{"series_id": "GDP", "value": 2.0}]}
        self.assertNotEqual(
            fingerprint, orchestrator.build_processor_fingerprint(processor, base)
        )

    def test_prompt_path_and_content_identity_change_fingerprint_without_exposing_text(
        self,
    ):
        processor = FingerprintedProcessor()
        base = {"llm": {"models": {"default": "provider/a"}}}
        first = orchestrator.build_processor_fingerprint(processor, base)

        same_content_new_path = {
            **base,
            "prompt_identity": {"path": "prompts/alternate.txt", "sha256": "a" * 64},
        }
        changed_content_same_path = {
            **base,
            "prompt_identity": {"path": "prompts/test.txt", "sha256": "b" * 64},
        }
        self.assertNotEqual(
            first,
            orchestrator.build_processor_fingerprint(processor, same_content_new_path),
        )
        self.assertNotEqual(
            first,
            orchestrator.build_processor_fingerprint(
                processor, changed_content_same_path
            ),
        )

        with patch.object(
            orchestrator, "canonical_fingerprint", wraps=canonical_fingerprint
        ) as canonical:
            self.assertEqual(
                first, orchestrator.build_processor_fingerprint(processor, base)
            )
        payload = canonical.call_args.args[0]
        self.assertEqual(payload["prompt_identity"]["path"], "prompts/test.txt")
        self.assertNotIn("raw prompt sentinel", str(payload))


class PromptIdentityTests(unittest.TestCase):
    def test_macro_and_briefing_identity_uses_runtime_resolution_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as config_dir:
            root = Path(config_dir)
            (root / "prompts").mkdir()
            raw = b"raw prompt sentinel\r\n{{value}}\n"
            (root / "prompts" / "one.txt").write_bytes(raw)
            (root / "prompts" / "two.txt").write_bytes(raw)

            with patch.dict(os.environ, {"CONFIG_DIR": config_dir}):
                for processor, processor_id in (
                    (MacroRegimeProcessor(), "macro_regime"),
                    (DailyBriefingProcessor(), "briefing"),
                ):
                    with self.subTest(processor=processor_id):
                        first_config = {
                            "processors": {
                                processor_id: {"prompt_template": "prompts/one.txt"}
                            }
                        }
                        second_config = {
                            "processors": {
                                processor_id: {"prompt_template": "prompts/two.txt"}
                            }
                        }
                        first = processor.get_prompt_identity(first_config)
                        self.assertEqual(first["path"], "prompts/one.txt")
                        self.assertEqual(
                            first["sha256"],
                            __import__("hashlib").sha256(raw).hexdigest(),
                        )
                        self.assertEqual(
                            first, processor.get_prompt_identity(first_config)
                        )
                        self.assertNotEqual(
                            first, processor.get_prompt_identity(second_config)
                        )

                        (root / "prompts" / "one.txt").write_bytes(raw + b"changed")
                        self.assertNotEqual(
                            first, processor.get_prompt_identity(first_config)
                        )
                        self.assertNotIn("raw prompt sentinel", str(first))
                        (root / "prompts" / "one.txt").write_bytes(raw)

    def test_absolute_prompt_identity_does_not_expose_home_path(self):
        with tempfile.TemporaryDirectory(dir=str(Path.home())) as config_dir:
            prompt = Path(config_dir) / "private-prompt.txt"
            prompt.write_bytes(b"safe")
            config = {"processors": {"macro_regime": {"prompt_template": str(prompt)}}}
            with patch.dict(os.environ, {"CONFIG_DIR": "/app"}):
                identity = MacroRegimeProcessor().get_prompt_identity(config)
        self.assertNotIn(str(Path.home()), str(identity))
        self.assertNotIn(config_dir, str(identity))


class ProcessorSkipRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.processor = FingerprintedProcessor()
        self.config = {"llm": {"models": {"default": "provider/a"}}}
        self.writes = patch.object(
            orchestrator, "insert_records", return_value=Mock()
        ).start()
        self.upserts = patch.object(
            orchestrator, "upsert_records", return_value=Mock()
        ).start()
        self.addCleanup(patch.stopall)

    def _run(self, prior=None, force=False, budget_context=None):
        with (
            patch.object(orchestrator, "get_processor", return_value=self.processor),
            patch.object(
                orchestrator, "_find_reusable_processor_output", return_value=prior
            ),
            patch.object(orchestrator, "_write_processing_log") as write_log,
        ):
            result = orchestrator._run_processor_impl(
                "macro_regime",
                self.config,
                "cid",
                manage_lifecycle=False,
                force=force,
                budget_context=budget_context,
            )
        return result, write_log

    def test_identical_successful_fingerprint_skips_without_processing_and_persists_history(
        self,
    ):
        result, write_log = self._run(prior={"output_id": "old-output"})

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "unchanged_inputs")
        self.assertTrue(result["reusable_output"])
        self.processor.process.assert_not_called()
        kwargs = write_log.call_args.kwargs
        self.assertEqual(kwargs["status"], "skipped")
        self.assertEqual(kwargs["skip_reason"], "unchanged_inputs")
        self.assertFalse(kwargs["forced"])
        self.assertIsNone(kwargs["output_id"])
        self.assertEqual(kwargs["tokens_input"], 0)
        self.assertEqual(kwargs["tokens_output"], 0)
        self.assertEqual(kwargs["cost_usd"], 0.0)

    def test_force_runs_same_fingerprint_and_is_separate_from_budget_authorization(
        self,
    ):
        context = BudgetContext(force=False, manual_authorized=False)
        result, write_log = self._run(
            prior={"output_id": "old-output"}, force=True, budget_context=context
        )

        self.assertEqual(result["status"], "success")
        self.processor.process.assert_called_once_with(
            self.config, "cid", budget_context=context
        )
        self.assertTrue(write_log.call_args.kwargs["forced"])
        self.assertIs(
            self.processor.process.call_args.kwargs["budget_context"], context
        )

    def test_non_forced_success_stores_fingerprint_with_null_skip_reason(self):
        result, write_log = self._run(prior=None)
        self.assertEqual(result["status"], "success")
        kwargs = write_log.call_args.kwargs
        self.assertRegex(kwargs["input_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertIsNone(kwargs["skip_reason"])
        self.assertFalse(kwargs["forced"])

    def test_fingerprint_lookup_failure_fails_safe_by_running(self):
        with (
            patch.object(orchestrator, "get_processor", return_value=self.processor),
            patch.object(
                orchestrator,
                "_find_reusable_processor_output",
                side_effect=RuntimeError("db down"),
            ),
            patch.object(orchestrator, "_write_processing_log"),
        ):
            result = orchestrator._run_processor_impl(
                "macro_regime", self.config, "cid", manage_lifecycle=False
            )
        self.assertEqual(result["status"], "success")
        self.processor.process.assert_called_once()

    def test_missing_prompt_fails_fingerprint_safe_without_reuse_lookup(self):
        self.processor.get_prompt_identity = Mock(
            side_effect=FileNotFoundError("Prompt template not found")
        )
        with (
            patch.object(orchestrator, "get_processor", return_value=self.processor),
            patch.object(orchestrator, "_find_reusable_processor_output") as lookup,
            patch.object(orchestrator, "_write_processing_log"),
        ):
            result = orchestrator._run_processor_impl(
                "macro_regime", self.config, "cid", manage_lifecycle=False
            )
        self.assertEqual(result["status"], "success")
        lookup.assert_not_called()
        self.processor.process.assert_called_once()

    def test_only_success_rows_are_queried_for_reuse(self):
        session = Mock()
        session.execute.return_value.fetchone.return_value = None
        with patch.object(orchestrator, "get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertIsNone(
                orchestrator._find_reusable_processor_output("macro_regime", "abc", {})
            )
        sql, params = session.execute.call_args.args
        self.assertIn("status = 'success'", str(sql))
        self.assertIn("input_fingerprint = :fingerprint", str(sql))
        self.assertEqual(params, {"processor": "macro_regime", "fingerprint": "abc"})


class BoundedProcessorInputTests(unittest.TestCase):
    def test_macro_inputs_include_all_revision_markers_consumed_by_trend_rules(self):
        processor = MacroRegimeProcessor()
        rows = [
            Row(
                series_id="GDP",
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                value=2.0,
                updated_at=datetime(2026, 1, 3, tzinfo=UTC),
            ),
            Row(
                series_id="GDP",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                value=1.0,
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ]
        session = Mock()
        session.execute.return_value = list(reversed(rows))
        with patch("processors.macro_regime.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            inputs = processor.get_fingerprint_inputs({})
        sql, params = session.execute.call_args.args
        self.assertIn("ROW_NUMBER()", str(sql))
        self.assertIn("observation_rank <= :history_limit", str(sql))
        self.assertEqual(params["ids"], sorted(params["ids"]))
        self.assertEqual(params["history_limit"], 15)
        self.assertEqual(
            inputs["observations"][0]["observed_at"],
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(inputs["observations"][1]["value"], 2.0)
        revised = dict(inputs)
        revised["observations"] = [dict(item) for item in inputs["observations"]]
        revised["observations"][1]["value"] = 2.1
        self.assertNotEqual(
            canonical_fingerprint(inputs), canonical_fingerprint(revised)
        )

    def test_briefing_inputs_include_latest_macro_calendar_window_and_watchlist_but_not_event_opinion(
        self,
    ):
        processor = DailyBriefingProcessor()
        window = {
            "today": datetime(2026, 1, 5).date(),
            "period_start": datetime(2026, 1, 5, tzinfo=UTC),
            "period_end": datetime(2026, 1, 9, 23, 59, tzinfo=UTC),
            "friday": datetime(2026, 1, 9).date(),
        }
        macro = Row(
            opinion_id="macro-1",
            opinion_created_at=datetime(2026, 1, 4, tzinfo=UTC),
            classification_id="class-1",
            classification_created_at=datetime(2026, 1, 4, tzinfo=UTC),
        )
        calendar = Row(
            event_count=1,
            latest_updated_at=datetime(2026, 1, 5, tzinfo=UTC),
            latest_scheduled_at=datetime(2026, 1, 6, tzinfo=UTC),
            max_event_id="event-1",
        )
        session = Mock()
        session.execute.side_effect = [
            Mock(fetchone=Mock(return_value=macro)),
            Mock(fetchone=Mock(return_value=calendar)),
        ]
        config = {"watchlist": {"trading": [{"symbol": "EUR_USD", "type": "forex"}]}}
        with (
            patch.object(processor, "_calendar_window", return_value=window),
            patch("processors.briefing.get_session") as get_session,
        ):
            get_session.return_value.__enter__.return_value = session
            inputs = processor.get_fingerprint_inputs(config)
        self.assertEqual(inputs["macro"]["opinion_id"], "macro-1")
        self.assertEqual(inputs["calendar"]["event_count"], 1)
        self.assertEqual(inputs["watchlist"], [{"symbol": "EUR_USD", "type": "forex"}])
        self.assertNotIn("event_impact", str(inputs))
        statements = [str(call.args[0]) for call in session.execute.call_args_list]
        self.assertTrue(
            all(
                "LIMIT" in statement or "COUNT(*)" in statement
                for statement in statements
            )
        )


class HealthySkippedStatusTests(unittest.TestCase):
    def test_skipped_is_healthy_in_aggregation(self):
        self.assertEqual(
            orchestrator.aggregate_stage_statuses(["success", "skipped"]), "success"
        )
        self.assertEqual(
            orchestrator.aggregate_stage_statuses(["skipped", "skipped"]), "success"
        )

    def test_skipped_dependency_is_satisfied_only_with_reusable_output(self):
        macro = Mock()
        macro.get_depends_on.return_value = ["fred"]
        briefing = Mock()
        briefing.get_depends_on.return_value = ["macro_regime"]
        config = {
            "processors": {
                "macro_regime": {"enabled": True},
                "briefing": {"enabled": True},
            }
        }

        with (
            patch.object(
                orchestrator,
                "get_all_processors",
                return_value={"macro_regime": macro, "briefing": briefing},
            ),
            patch.object(
                orchestrator,
                "run_processor",
                side_effect=[
                    {
                        "processor": "macro_regime",
                        "status": "skipped",
                        "reusable_output": False,
                    },
                ],
            ) as run,
        ):
            results = orchestrator._resolve_and_run_processors(config, "cid", {"fred"})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(results["briefing"]["status"], "skipped")
        self.assertIn("Dependencies not met", results["briefing"]["reason"])

        with (
            patch.object(
                orchestrator,
                "get_all_processors",
                return_value={"macro_regime": macro, "briefing": briefing},
            ),
            patch.object(
                orchestrator,
                "run_processor",
                side_effect=[
                    {
                        "processor": "macro_regime",
                        "status": "skipped",
                        "reusable_output": True,
                    },
                    {"processor": "briefing", "status": "success"},
                ],
            ) as run,
        ):
            results = orchestrator._resolve_and_run_processors(config, "cid", {"fred"})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(results["briefing"]["status"], "success")


if __name__ == "__main__":
    unittest.main()
