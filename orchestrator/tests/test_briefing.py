import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processors._validators import (
    validate_briefing_sections,
    coerce_briefing_fields,
)
from processors.briefing import DailyBriefingProcessor


WATCHLIST = [
    {"symbol": "EURUSD", "type": "forex"},
    {"symbol": "DXY", "type": "index"},
    {"symbol": "AUDJPY", "type": "forex"},
    {"symbol": "USDJPY", "type": "forex"},
    {"symbol": "SP500", "type": "index"},
    {"symbol": "XAUUSD", "type": "metal"},
    {"symbol": "XPTUSD", "type": "metal"},
    {"symbol": "GER40", "type": "index"},
    {"symbol": "UK100", "type": "index"},
]


CONFIG = {
    "timezone": {
        "primary": {"name": "Europe/London", "label": "London"},
        "secondary": {"name": "America/New_York", "label": "NY"},
    },
    "watchlist": {"trading": WATCHLIST},
}


class BriefingTests(unittest.TestCase):
    def test_timezone_formatting_uses_new_york_dst(self):
        processor = DailyBriefingProcessor()
        event = {
            "event_name": "ISM Services PMI",
            "country": "US",
            "scheduled_at": datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
            "impact_level": "high",
            "consensus": "53.0",
            "previous": "52.0",
            "metadata": {"currency": "USD"},
        }
        window = {
            "london_tz": ZoneInfo("Europe/London"),
            "ny_tz": ZoneInfo("America/New_York"),
            "london_label": "London",
            "ny_label": "NY",
        }

        prompt = processor._format_calendar_prompt([event], window)

        self.assertIn("13:00 London / 08:00 NY", prompt)
        self.assertIn("HIGH USD", prompt)

    def test_prompt_assembly_includes_calendar_and_watchlist(self):
        processor = DailyBriefingProcessor()
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "briefing_v4.txt"

        prompt = processor._build_prompt(
            template_path=str(prompt_path),
            current_date="Friday, May 08, 2026",
            macro_regime_summary="Risk-on but USD firm.",
            today_events="London 13:30 / NY 08:30 | HIGH USD | NFP",
            this_week_events="No high- or medium-impact relevant events scheduled.",
            watchlist=processor._format_watchlist(CONFIG),
        )

        self.assertIn("Risk-on but USD firm.", prompt)
        self.assertIn("HIGH USD | NFP", prompt)
        self.assertIn("EURUSD (forex)", prompt)
        self.assertIn("UK100 (index)", prompt)

    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_validation_requires_all_symbols_once_in_order(self):
        notes = [
            {
                "symbol": item["symbol"],
                "asset_class": item["type"],
                "bias": "mixed",
                "confidence": "moderate",
                "summary": "summary",
                "reason": "reason",
                "next_catalyst": "CPI, Thu 13:30",
                "note": "note",
            }
            for item in WATCHLIST
        ]
        valid, warnings = validate_briefing_sections(
            {"watchlist_notes": notes}, WATCHLIST
        )
        self.assertTrue(valid, warnings)

        invalid_notes = notes[:-1]
        valid, warnings = validate_briefing_sections(
            {"watchlist_notes": invalid_notes}, WATCHLIST
        )
        self.assertFalse(valid)
        self.assertTrue(any("UK100" in warning for warning in warnings))

    def test_empty_watchlist_handled_gracefully(self):
        """validate_briefing_sections handles empty watchlist_notes without crashing."""
        valid, warnings = validate_briefing_sections(
            {"watchlist_notes": []}, WATCHLIST
        )

        self.assertFalse(valid)
        # Should report all configured symbols as missing
        missing_warnings = [w for w in warnings if "missing" in w.lower()]
        self.assertGreater(len(missing_warnings), 0)
        for item in WATCHLIST:
            self.assertTrue(
                any(item["symbol"] in w for w in warnings),
                f"{item['symbol']} should be flagged as missing",
            )

    def test_missing_bias_field_gets_coerced(self):
        """coerce_briefing_fields defaults invalid bias values gracefully."""
        sections = {
            "watchlist_notes": [
                {
                    "symbol": "EURUSD",
                    "asset_class": "forex",
                    "bias": "sideways",  # invalid bias
                    "confidence": "moderate",
                    "summary": "test summary",
                    "note": "test note",
                },
                {
                    "symbol": "DXY",
                    "asset_class": "index",
                    # bias key intentionally missing
                    "confidence": "high",
                    "summary": "test summary",
                    "note": "test note",
                },
            ]
        }

        warnings = coerce_briefing_fields(sections)

        # "sideways" coerces to "neutral" via BIAS_COERCIONS
        self.assertEqual(sections["watchlist_notes"][0]["bias"], "neutral")
        # Missing bias is skipped gracefully (no crash, stays absent)
        self.assertNotIn("bias", sections["watchlist_notes"][1])
        # Should have logged a coercion warning for the invalid bias
        self.assertTrue(
            any("sideways" in w for w in warnings),
            "Should warn about coerced bias value",
        )

    @patch("processors.briefing.call_llm")
    @unittest.skip("skip: codex/market-intelligence-expansion contract not implemented in master")
    def test_empty_llm_response_triggers_retry(self, call_llm):
        """When validation fails, _validate_and_fix_sections retries the LLM call."""
        processor = DailyBriefingProcessor()

        valid_json = json.dumps(
            {
                "what_changed": "changed",
                "interpretation": "interpretation",
                "invalidation": "invalidation",
                "watchlist_notes": [
                    {
                        "symbol": w["symbol"],
                        "asset_class": w["type"],
                        "bias": "neutral",
                        "confidence": "moderate",
                        "summary": "s",
                        "reason": "r",
                        "next_catalyst": "CPI, Thu 13:30",
                        "note": "n",
                    }
                    for w in WATCHLIST
                ],
            }
        )

        # Retry LLM call returns valid data
        call_llm.return_value = {
            "content": valid_json,
            "model": "test-model",
            "tokens_input": 10,
            "tokens_output": 5,
            "cost_usd": 0.0,
        }

        # Sections with empty watchlist_notes will fail validation
        sections = {
            "what_changed": "",
            "interpretation": "",
            "invalidation": "",
            "watchlist_notes": [],
        }

        result = processor._validate_and_fix_sections(
            sections=sections,
            watchlist_config=WATCHLIST,
            prompt_text="test prompt",
            raw_response="test",
            llm_result={"content": "test", "model": "test"},
            model="test-model",
            config={
                "llm": {
                    "default_model": "test",
                    "api_key": "test-key",
                }
            },
            correlation_id="test-corr",
        )

        # call_llm should have been invoked for the retry
        call_llm.assert_called_once()
        # Retry should have produced valid watchlist_notes
        self.assertIn("watchlist_notes", result)
        self.assertGreater(len(result["watchlist_notes"]), 0)


if __name__ == "__main__":
    unittest.main()
