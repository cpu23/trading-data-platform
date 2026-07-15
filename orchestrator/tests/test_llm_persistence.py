import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
            "max_retries": 1,
        },
        "budgets": {"daily_llm_usd": 0},
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
    def test_processing_log_insert_defensively_nulls_legacy_raw_columns(self):
        import orchestrator

        session = Mock()
        session_context = Mock()
        session_context.__enter__ = Mock(return_value=session)
        session_context.__exit__ = Mock(return_value=False)
        now = datetime.now(timezone.utc)
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
        macro_data = {"GDP": {"latest": 2.0, "latest_date": "2026-01-02", "previous_date": "2026-01-01"}}

        with patch.object(processor, "_fetch_macro_data", return_value=macro_data), patch.object(
            processor, "_format_indicator_table", return_value="indicators"
        ), patch.object(processor, "_format_changes_table", return_value="changes"), patch.object(
            processor, "_build_cross_indicators", return_value={}
        ), patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL), patch(
            "llm_client.call_llm", side_effect=calls
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
        with patch.object(processor, "_fetch_upcoming_events", return_value=[{"event_name": "CPI"}]), patch.object(
            processor, "_format_watchlist", return_value="EURUSD"
        ), patch.object(processor, "_get_current_regime", return_value="neutral"), patch.object(
            processor, "_build_prompt", return_value=PROMPT_SENTINEL
        ), patch("llm_client.call_llm", side_effect=calls):
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
                "macro_trend": "structured briefing",
                "today": "today",
                "this_week": "week",
                "regime_assessment": "neutral",
                "watchlist_notes": [],
                "__raw_marker": RAW_SENTINEL,
            }
        )
        calls = [response(RAW_SENTINEL, 4, 1, 0.005), response(final, 8, 3, 0.015)]
        calendar = {"today_prompt": "none", "week_prompt": "none", "today_count": 0, "week_count": 0, "window": {}}
        with patch.object(processor, "_get_regime_summary", return_value=None), patch.object(
            processor, "_get_calendar_bundle", return_value=calendar
        ), patch.object(processor, "_format_watchlist", return_value="none"), patch.object(
            processor, "_build_prompt", return_value=PROMPT_SENTINEL
        ), patch.object(processor, "_get_latest_opinion_id", return_value=None), patch(
            "llm_client.call_llm", side_effect=calls
        ):
            result = processor.process(llm_config("briefing"), "cid")

        log = result["processing_log"]
        self.assertIsNone(log["prompt_text"])
        self.assertIsNone(log["raw_response"])
        self.assertEqual((log["tokens_input"], log["tokens_output"]), (12, 4))
        self.assertAlmostEqual(log["cost_usd"], 0.02)
        self.assertEqual(result["opinion"]["tokens_used"], 16)
        self.assertEqual(result["extra_records"]["daily_briefings"][0]["sections"]["macro_trend"], "structured briefing")
        self.assertNotIn(PROMPT_SENTINEL, str(log))
        self.assertNotIn(RAW_SENTINEL, str(log))

    def test_briefing_section_retry_persists_cumulative_stage_usage(self):
        processor = DailyBriefingProcessor()
        watchlist = [{"symbol": "EURUSD", "type": "forex"}]
        first = json.dumps(
            {
                "macro_trend": "first",
                "today": "today",
                "this_week": "week",
                "regime_assessment": "neutral",
                "watchlist_notes": [],
            }
        )
        final = json.dumps(
            {
                "macro_trend": "corrected",
                "today": "today",
                "this_week": "week",
                "regime_assessment": "neutral",
                "watchlist_notes": [{
                    "symbol": "EURUSD",
                    "asset_class": "forex",
                    "bias": "neutral",
                    "confidence": "moderate",
                    "summary": "safe summary",
                    "note": "safe note",
                }],
            }
        )
        calls = [response(first, 6, 2, 0.01), response(final, 9, 4, 0.02)]
        config = llm_config("briefing")
        config["watchlist"]["trading"] = watchlist
        calendar = {"today_prompt": "none", "week_prompt": "none", "today_count": 0, "week_count": 0, "window": {}}
        with patch.object(processor, "_get_regime_summary", return_value=None), patch.object(
            processor, "_get_calendar_bundle", return_value=calendar
        ), patch.object(processor, "_format_watchlist", return_value="EURUSD"), patch.object(
            processor, "_build_prompt", return_value=PROMPT_SENTINEL
        ), patch.object(processor, "_get_latest_opinion_id", return_value=None), patch(
            "llm_client.call_llm", side_effect=calls
        ):
            result = processor.process(config, "cid")

        log = result["processing_log"]
        self.assertEqual((log["tokens_input"], log["tokens_output"]), (15, 6))
        self.assertAlmostEqual(log["cost_usd"], 0.03)
        self.assertEqual(result["opinion"]["tokens_used"], 21)
        self.assertEqual(
            result["extra_records"]["daily_briefings"][0]["sections"]["macro_trend"],
            "corrected",
        )

    def test_briefing_section_retry_final_validation_failure_is_typed_with_usage(self):
        processor = DailyBriefingProcessor()
        first = json.dumps(
            {
                "macro_trend": "first",
                "today": "today",
                "this_week": "week",
                "regime_assessment": "neutral",
                "watchlist_notes": [],
            }
        )
        calls = [response(first, 6, 2, 0.01), response(RAW_SENTINEL, 9, 4, 0.02)]
        config = llm_config("briefing")
        config["watchlist"]["trading"] = [{"symbol": "EURUSD", "type": "forex"}]
        calendar = {"today_prompt": "none", "week_prompt": "none", "today_count": 0, "week_count": 0, "window": {}}
        with patch.object(processor, "_get_regime_summary", return_value=None), patch.object(
            processor, "_get_calendar_bundle", return_value=calendar
        ), patch.object(processor, "_format_watchlist", return_value="EURUSD"), patch.object(
            processor, "_build_prompt", return_value=PROMPT_SENTINEL
        ), patch("llm_client.call_llm", side_effect=calls), patch("processors.briefing.logger") as briefing_logger:
            with self.assertRaises(LLMValidationError) as raised:
                processor.process(config, "cid")

        self.assertEqual(raised.exception.telemetry.tokens_input_total, 15)
        self.assertAlmostEqual(raised.exception.telemetry.cost_usd_total, 0.03)
        self.assertNotIn(RAW_SENTINEL, str(briefing_logger.mock_calls))

    def test_event_impact_schema_failure_is_typed_and_preserves_telemetry(self):
        processor = EventImpactProcessor()
        calls = [response("{}", 2, 1, 0.01), response("{}", 3, 1, 0.02)]
        with patch.object(processor, "_fetch_upcoming_events", return_value=[{"event_name": "CPI"}]), patch.object(
            processor, "_format_watchlist", return_value="EURUSD"
        ), patch.object(processor, "_get_current_regime", return_value="neutral"), patch.object(
            processor, "_build_prompt", return_value=PROMPT_SENTINEL
        ), patch("llm_client.call_llm", side_effect=calls):
            with self.assertRaises(LLMValidationError) as raised:
                processor.process(llm_config("event_impact"), "cid")

        self.assertEqual(raised.exception.code, "llm_validation_failed")
        self.assertEqual(raised.exception.telemetry.tokens_input_total, 5)
        self.assertAlmostEqual(raised.exception.telemetry.cost_usd_total, 0.03)

    def test_event_impact_invalid_after_retry_is_typed_safe_and_persisted_with_usage(self):
        import orchestrator

        processor = EventImpactProcessor()
        calls = [response(RAW_SENTINEL, 5, 2, 0.01), response(RAW_SENTINEL, 6, 3, 0.02)]
        with patch.object(orchestrator, "get_processor", return_value=processor), patch.object(
            orchestrator, "build_processor_fingerprint", return_value=None
        ), patch.object(orchestrator, "_write_processing_log") as write_log, patch.object(
            processor, "_fetch_upcoming_events", return_value=[{"event_name": "CPI"}]
        ), patch.object(processor, "_format_watchlist", return_value="EURUSD"), patch.object(
            processor, "_get_current_regime", return_value="neutral"
        ), patch.object(processor, "_build_prompt", return_value=PROMPT_SENTINEL), patch(
            "llm_client.call_llm", side_effect=calls
        ), patch("processors.event_impact.logger") as event_logger:
            result = orchestrator._run_processor_impl(
                "event_impact", llm_config("event_impact"), "cid", manage_lifecycle=False
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
            with patch.object(processor, "_fetch_upcoming_events", return_value=[{"event_name": "CPI"}]), patch.object(
                processor, "_format_watchlist", return_value="EURUSD"
            ), patch.object(processor, "_get_current_regime", return_value="neutral"), patch.object(
                processor, "_build_prompt", return_value=PROMPT_SENTINEL
            ), patch("llm_client.call_llm", side_effect=calls):
                processor.process(llm_config("event_impact"), "cid")
        self.assertEqual(raised.exception.code, "llm_validation_failed")
        self.assertEqual(raised.exception.telemetry.tokens_input_total, 11)
        self.assertNotIn(RAW_SENTINEL, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
