import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processors._validators import (
    OutputPolicyError,
    coerce_briefing_fields,
    scan_prohibited_language,
    validate_briefing_sections,
    validate_event_impact_output,
    validate_macro_regime_output,
)
from processors.briefing import DailyBriefingProcessor
from processors.event_impact import EventImpactProcessor
from processors.macro_regime import MacroRegimeProcessor


WATCHLIST = [
    {"symbol": "EURUSD", "type": "forex"},
    {"symbol": "SP500", "type": "index"},
]
EVENTS = [
    {
        "event_name": "US CPI",
        "scheduled_at": "2026-06-19T12:30:00+00:00",
        "consensus": "2.7%",
        "previous": "2.8%",
    }
]


def valid_briefing():
    return {
        "macro_trend": "Growth is slowing while inflation remains sticky.",
        "today": "US inflation is the dominant scheduled catalyst.",
        "this_week": "Central-bank communication may alter policy expectations.",
        "regime_assessment": "Restrictive policy and cautious positioning limit risk appetite.",
        "watchlist_notes": [
            {
                "symbol": "EURUSD",
                "asset_class": "forex",
                "bias": "bearish",
                "confidence": "moderate",
                "summary": "Relative policy expectations favor the dollar.",
                "note": "Sticky US inflation and cautious positioning support dollar demand.",
            },
            {
                "symbol": "SP500",
                "asset_class": "index",
                "bias": "mixed",
                "confidence": "low",
                "summary": "Earnings resilience offsets restrictive policy.",
                "note": "Fundamentals remain firm, but policy expectations constrain valuation support.",
            },
        ],
    }


def valid_macro():
    return {
        "regime": "slowdown",
        "sub_regime": "policy_hold",
        "direction": "mixed",
        "confidence": "moderate",
        "timeframe": "medium_term",
        "summary": "Growth is slowing while inflation remains above objective.",
        "key_factors": ["Restrictive policy", "Cooling labor demand"],
        "reasoning": "The supplied data indicate softer growth and persistent inflation.",
        "market_implications": "Rate expectations support the dollar while limiting equity valuation expansion.",
        "caution_flags": ["A renewed inflation acceleration"],
    }


def valid_event():
    return {
        "events": [
            {
                "event_name": "US CPI",
                "scheduled_at": "2026-06-19T12:30:00+00:00",
                "consensus": "2.7%",
                "previous": "2.8%",
                "context": "Inflation shapes Federal Reserve policy expectations.",
                "consensus_met_scenario": {
                    "direction": "neutral",
                    "volatility": "moderate",
                    "narrative": "An in-line result would broadly preserve current rate expectations.",
                },
                "upside_surprise_scenario": {
                    "direction": "bullish_usd",
                    "volatility": "high",
                    "narrative": "A hotter result would reinforce restrictive policy expectations.",
                },
                "downside_surprise_scenario": {
                    "direction": "bearish_usd",
                    "volatility": "high",
                    "narrative": "A softer result would increase the probability of policy easing.",
                },
                "affected_instruments": [
                    {
                        "symbol": "EURUSD",
                        "sensitivity": "high",
                        "expected_reaction": "Relative rate expectations would drive dollar sensitivity.",
                    }
                ],
                "market_implications": "The release is the principal near-term policy catalyst.",
            }
        ],
        "overall_volatility_outlook": "Event-driven volatility is likely to be elevated.",
        "catalyst_summary": "US inflation is the dominant policy catalyst.",
    }


class PassOneValidatorTests(unittest.TestCase):
    def test_scanner_allows_bias_positioning_and_catalysts(self):
        findings = scan_prohibited_language(
            "Institutional positioning and policy catalysts support a bullish bias."
        )
        self.assertEqual(findings, [])

    def test_scanner_allows_economic_entry_and_allocation_language(self):
        findings = scan_prohibited_language(
            "High barriers to entry constrain supply, while corporate capital "
            "allocation remains focused on industrial capacity."
        )
        self.assertEqual(findings, [])

    def test_scanner_rejects_execution_and_technical_analysis(self):
        findings = scan_prohibited_language(
            "Buy at support with a stop-loss after the RSI breakout."
        )
        self.assertGreaterEqual(len(findings), 3)
        self.assertTrue(any("trading_instruction" in item for item in findings))
        self.assertTrue(any("stop_target" in item for item in findings))
        self.assertTrue(any("technical_analysis" in item for item in findings))

    def test_briefing_is_coerced_before_strict_revalidation(self):
        briefing = valid_briefing()
        briefing["watchlist_notes"][0]["bias"] = "sideways"
        coerce_briefing_fields(briefing)
        valid, issues = validate_briefing_sections(briefing, WATCHLIST)
        self.assertTrue(valid, issues)
        self.assertEqual(briefing["watchlist_notes"][0]["bias"], "neutral")

    def test_strict_schemas_reject_extra_keys_and_policy_language(self):
        macro = valid_macro()
        macro["extra"] = "not allowed"
        macro["reasoning"] = "Enter after the moving average confirms the move."
        valid, issues = validate_macro_regime_output(macro)
        self.assertFalse(valid)
        self.assertTrue(any("unexpected keys" in issue for issue in issues))
        self.assertTrue(any("technical_analysis" in issue for issue in issues))
        self.assertTrue(any("trading_instruction" in issue for issue in issues))

        event = valid_event()
        valid, issues = validate_event_impact_output(event, EVENTS, WATCHLIST)
        self.assertTrue(valid, issues)
        event["events"][0]["market_implications"] = "Allocate capital after the release."
        valid, issues = validate_event_impact_output(event, EVENTS, WATCHLIST)
        self.assertFalse(valid)
        self.assertTrue(any("sizing_allocation" in issue for issue in issues))


class PassOneRepairTests(unittest.TestCase):
    @patch("processors.briefing.call_llm")
    def test_briefing_repairs_once_then_quarantines(self, call_llm):
        call_llm.return_value = {
            "content": json.dumps(
                {
                    **valid_briefing(),
                    "macro_trend": "Buy after the breakout.",
                }
            ),
            "model": "test",
            "tokens_input": 1,
            "tokens_output": 1,
            "cost_usd": 0.0,
        }
        with self.assertRaises(OutputPolicyError):
            DailyBriefingProcessor()._validate_and_fix_sections(
                sections={
                    **valid_briefing(),
                    "today": "Use RSI before entry.",
                },
                watchlist_config=WATCHLIST,
                prompt_text="prompt",
                raw_response="invalid",
                llm_result={"content": "invalid"},
                model="test",
                config={},
                correlation_id="corr",
            )
        call_llm.assert_called_once()

    @patch("processors.event_impact.call_llm")
    def test_event_impact_repairs_once_then_quarantines(self, call_llm):
        invalid = valid_event()
        invalid["catalyst_summary"] = "Set a price target after CPI."
        call_llm.return_value = {
            "content": json.dumps(invalid),
            "model": "test",
            "tokens_input": 1,
            "tokens_output": 1,
            "cost_usd": 0.0,
        }
        processor = EventImpactProcessor()
        with self.assertRaises(OutputPolicyError):
            processor._validate_and_repair_output(
                raw_response=json.dumps(invalid),
                prompt_text="prompt",
                llm_result={"content": json.dumps(invalid)},
                expected_events=EVENTS,
                watchlist_config=WATCHLIST,
                model="test",
                config={},
                correlation_id="corr",
            )
        call_llm.assert_called_once()

    @patch("processors.macro_regime.call_llm")
    def test_macro_repairs_once_and_adopts_only_valid_repair(self, call_llm):
        initial = valid_macro()
        initial["reasoning"] = "Enter after technical analysis confirms the move."
        repaired = valid_macro()
        call_llm.return_value = {
            "content": json.dumps(repaired),
            "model": "repaired-model",
            "tokens_input": 4,
            "tokens_output": 5,
            "cost_usd": 0.25,
        }
        llm_result = {
            "content": json.dumps(initial),
            "model": "initial-model",
            "tokens_input": 1,
            "tokens_output": 2,
            "cost_usd": 0.1,
        }
        result = MacroRegimeProcessor()._validate_and_repair_output(
            raw_response=llm_result["content"],
            prompt_text="prompt",
            llm_result=llm_result,
            model="test",
            config={},
            correlation_id="corr",
        )
        self.assertEqual(result, repaired)
        self.assertEqual(llm_result["content"], json.dumps(repaired))
        self.assertEqual(llm_result["tokens_input"], 5)
        self.assertEqual(llm_result["tokens_output"], 7)
        self.assertAlmostEqual(llm_result["cost_usd"], 0.35)
        call_llm.assert_called_once()


class PassOnePromptTests(unittest.TestCase):
    def test_all_prompts_state_economics_only_policy(self):
        prompt_dir = Path(__file__).resolve().parents[2] / "prompts"
        for prompt_path in prompt_dir.glob("*.txt"):
            text = prompt_path.read_text().lower()
            with self.subTest(prompt=prompt_path.name):
                self.assertIn("economics-only", text)
                self.assertIn("positioning", text)
                self.assertIn("technical analysis", text)
                self.assertIn("trading instructions", text)
                self.assertIn("position sizing", text)
                self.assertIn("allocation", text)


if __name__ == "__main__":
    unittest.main()
