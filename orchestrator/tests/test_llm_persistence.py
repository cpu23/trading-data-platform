import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import WriteResult
from llm_client import LLMValidationError
from processors.briefing import DailyBriefingProcessor
from processors.event_impact import EventImpactProcessor
from processors.macro_regime import MacroRegimeProcessor

PROMPT_SENTINEL = "PRIVATE-PROMPT-SENTINEL-8"
RAW_SENTINEL = "RAW-MODEL-SENTINEL-8"


def llm_config(processor_id: str) -> dict:
    return {
        "llm": {
            "api_key": "not-a-real-key",
            "default_model": "provider/default",
            "models": {processor_id: f"provider/{processor_id}"},
            "max_output_tokens": {processor_id: 1000},
            "temperatures": {processor_id: 0.2},
            "structured_response": {processor_id: True},
            "stage_timeout_seconds": 90,
            "validation_retries": 1,
        },
        "budgets": {"daily_llm_usd": 2.0},
        "watchlist": {"trading": []},
    }


def response(content: str, tokens_input: int, tokens_output: int, cost: float) -> dict:
    return {
        "content": content,
        "model": "provider/test",
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "cost_usd": cost,
    }


class ProcessorLLMPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Budget reservations need a live DB; stub the quota reservation so
        # the LLM stage admits calls under the positive test cap without one.
        cls._budget_patch = patch(
            "budgets._reserve_budget_quota", return_value="reservation-1"
        )
        cls._budget_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._budget_patch.stop()

    def _successful_processor_result(self, *, processing_log=None, extra_records=None):
        return {
            "opinion": {"opinion_id": "11111111-1111-1111-1111-111111111111"},
            "extra_records": extra_records or {},
            "processing_log": processing_log
            if processing_log is not None
            else {
                "status": "success",
                "output_id": "untrusted-output-id",
                "model_used": "provider/cumulative",
                "tokens_input": 31,
                "tokens_output": 7,
                "cost_usd": 0.125,
                "input_summary": {"attempt_count": 2, "safe_stage": "validation_retry"},
                "prompt_text": PROMPT_SENTINEL,
                "raw_response": RAW_SENTINEL,
            },
        }

    def _run_processor_with_writes(
        self, processor_result, *, opinion_write, extra_write=None
    ):
        import orchestrator

        processor = Mock()
        opinion_write_effect = (
            {"side_effect": opinion_write}
            if isinstance(opinion_write, Exception)
            else {"return_value": opinion_write}
        )
        processor.process.return_value = processor_result
        with (
            patch.object(orchestrator, "get_processor", return_value=processor),
            patch.object(
                orchestrator, "build_processor_fingerprint", return_value="f" * 64
            ),
            patch.object(
                orchestrator, "insert_records", **opinion_write_effect
            ) as insert,
            patch.object(
                orchestrator,
                "upsert_records",
                return_value=extra_write or WriteResult(0, 0, 0, ()),
            ),
            patch.object(orchestrator, "_write_processing_log") as write_log,
        ):
            result = orchestrator._run_processor_impl(
                "briefing", config={}, correlation_id="cid", manage_lifecycle=False
            )
        return result, write_log, insert

    def test_partial_opinion_write_retains_exact_cumulative_llm_usage_once(self):
        result, write_log, _ = self._run_processor_with_writes(
            self._successful_processor_result(),
            opinion_write=WriteResult(2, 1, 1, ("one opinion row failed",)),
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["opinion_id"], "11111111-1111-1111-1111-111111111111")
        write_log.assert_called_once()
        logged = write_log.call_args.kwargs
        self.assertEqual(logged["status"], "partial")
        self.assertEqual(logged["input_fingerprint"], "f" * 64)
        self.assertEqual(logged["output_id"], result["opinion_id"])
        self.assertEqual((logged["tokens_input"], logged["tokens_output"]), (31, 7))
        self.assertEqual(logged["cost_usd"], 0.125)
        self.assertEqual(logged["model_used"], "provider/cumulative")
        self.assertEqual(
            logged["input_summary"],
            {"attempt_count": 2, "safe_stage": "validation_retry"},
        )
        self.assertIsNone(logged["prompt_text"])
        self.assertIsNone(logged["raw_response"])

    def test_failed_opinion_write_has_no_false_output_id_but_keeps_usage(self):
        result, write_log, _ = self._run_processor_with_writes(
            self._successful_processor_result(),
            opinion_write=WriteResult(1, 0, 1, ("opinion not written",)),
        )

        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["opinion_id"])
        logged = write_log.call_args.kwargs
        self.assertEqual(logged["status"], "failed")
        self.assertIsNone(logged["output_id"])
        self.assertEqual((logged["tokens_input"], logged["tokens_output"]), (31, 7))
        self.assertEqual(logged["cost_usd"], 0.125)

    def test_raised_opinion_write_is_retryable_persistence_failure(self):
        result, write_log, _ = self._run_processor_with_writes(
            self._successful_processor_result(),
            opinion_write=RuntimeError("database unavailable"),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_class"], "persistence")
        self.assertTrue(result["retryable"])
        logged = write_log.call_args.kwargs
        self.assertEqual(logged["status"], "failed")
        self.assertIn("DB write failed", logged["error_message"])

    def test_partial_extra_write_after_durable_opinion_keeps_usage_once(self):
        processor_result = self._successful_processor_result(
            extra_records={"daily_briefings": [{"briefing_date": "2026-07-15"}]}
        )
        result, write_log, insert = self._run_processor_with_writes(
            processor_result,
            opinion_write=WriteResult(1, 1, 0, ()),
            extra_write=WriteResult(2, 1, 1, ("one briefing row failed",)),
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["opinion_id"], "11111111-1111-1111-1111-111111111111")
        insert.assert_called_once()
        write_log.assert_called_once()
        logged = write_log.call_args.kwargs
        self.assertEqual(logged["status"], "partial")
        self.assertEqual(logged["output_id"], result["opinion_id"])
        self.assertEqual((logged["tokens_input"], logged["tokens_output"]), (31, 7))
        self.assertEqual(logged["cost_usd"], 0.125)

    def test_malformed_processing_log_fails_safe_without_crashing(self):
        result, write_log, _ = self._run_processor_with_writes(
            self._successful_processor_result(processing_log=["malformed"]),
            opinion_write=WriteResult(1, 0, 1, ("opinion not written",)),
        )

        self.assertEqual(result["status"], "failed")
        logged = write_log.call_args.kwargs
        self.assertEqual(logged["status"], "failed")
        self.assertIsNone(logged["output_id"])
        self.assertEqual(logged["tokens_input"], 0)
        self.assertEqual(logged["tokens_output"], 0)
        self.assertEqual(logged["cost_usd"], 0.0)
        self.assertIsNone(logged["input_summary"])
        self.assertIsNone(logged["model_used"])

    def test_malformed_processing_telemetry_uses_safe_zero_fields(self):
        malformed = {
            "status": "success",
            "output_id": "false-id",
            "tokens_input": "31",
            "tokens_output": object(),
            "cost_usd": float("nan"),
            "model_used": {"unsafe": "model"},
            "input_summary": "unsafe summary",
        }
        result, write_log, _ = self._run_processor_with_writes(
            self._successful_processor_result(processing_log=malformed),
            opinion_write=WriteResult(1, 0, 1, ("opinion not written",)),
        )

        self.assertEqual(result["status"], "failed")
        logged = write_log.call_args.kwargs
        self.assertEqual(logged["tokens_input"], 0)
        self.assertEqual(logged["tokens_output"], 0)
        self.assertEqual(logged["cost_usd"], 0.0)
        self.assertIsNone(logged["model_used"])
        self.assertIsNone(logged["input_summary"])
        self.assertIsNone(logged["output_id"])

    def test_processing_log_insert_defensively_nulls_legacy_raw_columns(self):
        import orchestrator

        session = Mock()
        session_context = Mock()
        session_context.__enter__ = Mock(return_value=session)
        session_context.__exit__ = Mock(return_value=False)
        now = datetime.now(UTC)
        with patch.object(orchestrator, "get_session", return_value=session_context):
            orchestrator._write_processing_log(
                processor_id="macro_regime",
                started_at=now,
                completed_at=now,
                status="success",
                input_summary={"safe": True},
                output_id="opinion-id",
                prompt_text=PROMPT_SENTINEL,
                raw_response=RAW_SENTINEL,
                model_used="provider/test",
                tokens_input=1,
                tokens_output=2,
                cost_usd=0.01,
                duration_ms=3,
                error_message=None,
                input_fingerprint=None,
                skip_reason=None,
                forced=False,
                config={},
                correlation_id="cid",
            )

        inserted = session.execute.call_args.args[1]
        self.assertIsNone(inserted["prompt_text"])
        self.assertIsNone(inserted["raw_response"])
        self.assertNotIn(PROMPT_SENTINEL, str(inserted))
        self.assertNotIn(RAW_SENTINEL, str(inserted))

    def test_macro_json_retry_persists_cumulative_usage_without_raw_data(self):
        processor = MacroRegimeProcessor()
        final = json.dumps(
            {
                "summary": "structured macro result",
                "reasoning": "structured reasoning",
                "regime": "quiet",
                "__raw_marker": RAW_SENTINEL,
            }
        )
        calls = [response(RAW_SENTINEL, 10, 2, 0.01), response(final, 20, 4, 0.02)]
        macro_data = {
            "GDP": {
                "latest": 2.0,
                "latest_date": "2026-01-02",
                "previous_date": "2026-01-01",
            }
        }

        with (
            patch.object(processor, "_fetch_macro_data", return_value=macro_data),
            patch.object(
                processor, "_format_indicator_table", return_value="indicators"
            ),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch("llm_client.call_llm", side_effect=calls),
        ):
            result = processor.process(llm_config("macro_regime"), "cid")

        log = result["processing_log"]
        self.assertIsNone(log["prompt_text"])
        self.assertIsNone(log["raw_response"])
        self.assertEqual((log["tokens_input"], log["tokens_output"]), (30, 6))
        self.assertAlmostEqual(log["cost_usd"], 0.03)
        self.assertEqual(result["opinion"]["tokens_used"], 36)
        self.assertAlmostEqual(result["opinion"]["cost_usd"], 0.03)
        self.assertEqual(result["opinion"]["summary"], "structured macro result")
        self.assertNotIn(PROMPT_SENTINEL, str(log))
        self.assertNotIn(RAW_SENTINEL, str(log))

    def test_event_impact_retry_persists_cumulative_usage_without_raw_data(self):
        processor = EventImpactProcessor()
        final = json.dumps(
            {
                "events": [],
                "overall_volatility_outlook": "structured outlook",
                "risk_management_note": "structured note",
                "__raw_marker": RAW_SENTINEL,
            }
        )
        calls = [response(RAW_SENTINEL, 3, 1, 0.004), response(final, 7, 2, 0.006)]
        with (
            patch.object(
                processor,
                "_fetch_upcoming_events",
                return_value=[{"event_name": "CPI"}],
            ),
            patch.object(processor, "_format_watchlist", return_value="EURUSD"),
            patch.object(processor, "_get_current_regime", return_value="neutral"),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch("llm_client.call_llm", side_effect=calls),
        ):
            result = processor.process(llm_config("event_impact"), "cid")

        log = result["processing_log"]
        self.assertIsNone(log["prompt_text"])
        self.assertIsNone(log["raw_response"])
        self.assertEqual((log["tokens_input"], log["tokens_output"]), (10, 3))
        self.assertAlmostEqual(log["cost_usd"], 0.01)
        self.assertEqual(result["opinion"]["tokens_used"], 13)
        self.assertNotIn(PROMPT_SENTINEL, str(log))
        self.assertNotIn(RAW_SENTINEL, str(log))

    def test_briefing_json_retry_persists_cumulative_usage_without_raw_data(self):
        processor = DailyBriefingProcessor()
        final = json.dumps(
            {
                "what_changed": "structured briefing",
                "interpretation": "interpretation",
                "invalidation": "invalidation",
                "watchlist_notes": [],
                "__raw_marker": RAW_SENTINEL,
            }
        )
        calls = [response(RAW_SENTINEL, 4, 1, 0.005), response(final, 8, 3, 0.015)]
        calendar = {
            "today_prompt": "none",
            "week_prompt": "none",
            "today_count": 0,
            "week_count": 0,
            "window": {},
        }
        with (
            patch.object(processor, "_get_regime_summary", return_value=None),
            patch.object(processor, "_get_calendar_bundle", return_value=calendar),
            patch.object(processor, "_format_watchlist", return_value="none"),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch.object(processor, "_get_latest_opinion_id", return_value=None),
            patch("llm_client.call_llm", side_effect=calls),
        ):
            result = processor.process(llm_config("briefing"), "cid")

        log = result["processing_log"]
        self.assertIsNone(log["prompt_text"])
        self.assertIsNone(log["raw_response"])
        self.assertEqual((log["tokens_input"], log["tokens_output"]), (12, 4))
        self.assertAlmostEqual(log["cost_usd"], 0.02)
        self.assertEqual(result["opinion"]["tokens_used"], 16)
        self.assertEqual(
            result["extra_records"]["daily_briefings"][0]["sections"]["what_changed"],
            "structured briefing",
        )
        self.assertEqual(
            result["extra_records"]["daily_briefings"][0]["correlation_id"],
            "cid",
        )
        self.assertNotIn(PROMPT_SENTINEL, str(log))
        self.assertNotIn(RAW_SENTINEL, str(log))

    def test_briefing_section_retry_persists_cumulative_stage_usage(self):
        processor = DailyBriefingProcessor()
        watchlist = [{"symbol": "EURUSD", "type": "forex"}]
        first = json.dumps(
            {
                "what_changed": "first",
                "interpretation": "interpretation",
                "invalidation": "invalidation",
                "watchlist_notes": [],
            }
        )
        final = json.dumps(
            {
                "what_changed": "corrected",
                "interpretation": "interpretation",
                "invalidation": "invalidation",
                "watchlist_notes": [
                    {
                        "symbol": "EURUSD",
                        "asset_class": "forex",
                        "bias": "neutral",
                        "confidence": "moderate",
                        "summary": "safe summary",
                        "reason": "safe reason",
                        "next_catalyst": "CPI, Thu 13:30",
                        "note": "safe note",
                    }
                ],
            }
        )
        calls = [response(first, 6, 2, 0.01), response(final, 9, 4, 0.02)]
        config = llm_config("briefing")
        config["watchlist"]["trading"] = watchlist
        calendar = {
            "today_prompt": "none",
            "week_prompt": "none",
            "today_count": 0,
            "week_count": 0,
            "window": {},
        }
        with (
            patch.object(processor, "_get_regime_summary", return_value=None),
            patch.object(processor, "_get_calendar_bundle", return_value=calendar),
            patch.object(processor, "_format_watchlist", return_value="EURUSD"),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch.object(processor, "_get_latest_opinion_id", return_value=None),
            patch("llm_client.call_llm", side_effect=calls),
        ):
            result = processor.process(config, "cid")

        log = result["processing_log"]
        self.assertEqual((log["tokens_input"], log["tokens_output"]), (15, 6))
        self.assertAlmostEqual(log["cost_usd"], 0.03)
        self.assertEqual(result["opinion"]["tokens_used"], 21)
        self.assertEqual(
            result["extra_records"]["daily_briefings"][0]["sections"]["what_changed"],
            "corrected",
        )

    def test_briefing_section_retry_final_validation_failure_is_typed_with_usage(self):
        processor = DailyBriefingProcessor()
        first = json.dumps(
            {
                "what_changed": "first",
                "interpretation": "interpretation",
                "invalidation": "invalidation",
                "watchlist_notes": [],
            }
        )
        calls = [response(first, 6, 2, 0.01), response(RAW_SENTINEL, 9, 4, 0.02)]
        config = llm_config("briefing")
        config["watchlist"]["trading"] = [{"symbol": "EURUSD", "type": "forex"}]
        calendar = {
            "today_prompt": "none",
            "week_prompt": "none",
            "today_count": 0,
            "week_count": 0,
            "window": {},
        }
        with (
            patch.object(processor, "_get_regime_summary", return_value=None),
            patch.object(processor, "_get_calendar_bundle", return_value=calendar),
            patch.object(processor, "_format_watchlist", return_value="EURUSD"),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch("llm_client.call_llm", side_effect=calls),
            patch("processors.briefing.logger") as briefing_logger,
        ):
            with self.assertRaises(LLMValidationError) as raised:
                processor.process(config, "cid")

        self.assertEqual(raised.exception.telemetry.tokens_input_total, 15)
        self.assertAlmostEqual(raised.exception.telemetry.cost_usd_total, 0.03)
        self.assertNotIn(RAW_SENTINEL, str(briefing_logger.mock_calls))

    def test_event_impact_schema_failure_is_typed_and_preserves_telemetry(self):
        processor = EventImpactProcessor()
        calls = [response("{}", 2, 1, 0.01), response("{}", 3, 1, 0.02)]
        with (
            patch.object(
                processor,
                "_fetch_upcoming_events",
                return_value=[{"event_name": "CPI"}],
            ),
            patch.object(processor, "_format_watchlist", return_value="EURUSD"),
            patch.object(processor, "_get_current_regime", return_value="neutral"),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch("llm_client.call_llm", side_effect=calls),
        ):
            with self.assertRaises(LLMValidationError) as raised:
                processor.process(llm_config("event_impact"), "cid")

        self.assertEqual(raised.exception.code, "llm_validation_failed")
        self.assertEqual(raised.exception.telemetry.tokens_input_total, 5)
        self.assertAlmostEqual(raised.exception.telemetry.cost_usd_total, 0.03)

    def test_event_impact_invalid_after_retry_is_typed_safe_and_persisted_with_usage(
        self,
    ):
        import orchestrator

        processor = EventImpactProcessor()
        calls = [response(RAW_SENTINEL, 5, 2, 0.01), response(RAW_SENTINEL, 6, 3, 0.02)]
        with (
            patch.object(orchestrator, "get_processor", return_value=processor),
            patch.object(
                orchestrator, "build_processor_fingerprint", return_value=None
            ),
            patch.object(orchestrator, "_write_processing_log") as write_log,
            patch.object(
                processor,
                "_fetch_upcoming_events",
                return_value=[{"event_name": "CPI"}],
            ),
            patch.object(processor, "_format_watchlist", return_value="EURUSD"),
            patch.object(processor, "_get_current_regime", return_value="neutral"),
            patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
            patch("llm_client.call_llm", side_effect=calls),
            patch("processors.event_impact.logger") as event_logger,
        ):
            result = orchestrator._run_processor_impl(
                "event_impact",
                llm_config("event_impact"),
                "cid",
                manage_lifecycle=False,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "LLM response validation failed after retry")
        logged = write_log.call_args.kwargs
        self.assertEqual((logged["tokens_input"], logged["tokens_output"]), (11, 5))
        self.assertAlmostEqual(logged["cost_usd"], 0.03)
        self.assertIsNone(logged["prompt_text"])
        self.assertIsNone(logged["raw_response"])
        self.assertNotIn(PROMPT_SENTINEL, str(logged))
        self.assertNotIn(RAW_SENTINEL, str(logged))
        self.assertNotIn(RAW_SENTINEL, str(event_logger.mock_calls))

        with self.assertRaises(LLMValidationError) as raised:
            # Assert the public typed contract independently of orchestration's safe conversion.
            with (
                patch.object(
                    processor,
                    "_fetch_upcoming_events",
                    return_value=[{"event_name": "CPI"}],
                ),
                patch.object(processor, "_format_watchlist", return_value="EURUSD"),
                patch.object(processor, "_get_current_regime", return_value="neutral"),
                patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL),
                patch("llm_client.call_llm", side_effect=calls),
            ):
                processor.process(llm_config("event_impact"), "cid")
        self.assertEqual(raised.exception.code, "llm_validation_failed")
        self.assertEqual(raised.exception.telemetry.tokens_input_total, 11)
        self.assertNotIn(RAW_SENTINEL, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
