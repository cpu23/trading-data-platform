import json
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.runtime_config import WatchlistInstrumentConfig
from processors._validators import (
    coerce_briefing_fields,
    validate_briefing_sections,
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
            "scheduled_at": datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
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

    def test_response_schema_enforces_briefing_shape_and_enums(self):
        schema = DailyBriefingProcessor._response_schema(WATCHLIST)["schema"]

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["required"],
            ["what_changed", "interpretation", "invalidation", "watchlist_notes"],
        )
        note_schema = schema["properties"]["watchlist_notes"]["items"]
        self.assertFalse(note_schema["additionalProperties"])
        self.assertEqual(
            note_schema["properties"]["symbol"]["enum"],
            [item["symbol"] for item in WATCHLIST],
        )
        self.assertEqual(
            note_schema["properties"]["asset_class"]["enum"],
            ["forex", "index", "metal"],
        )

    def test_response_schema_consumes_frozen_runtime_watchlist(self):
        configured = [
            WatchlistInstrumentConfig(symbol="EURUSD", type="forex"),
            WatchlistInstrumentConfig(symbol="SP500", type="index"),
        ]

        schema = DailyBriefingProcessor._response_schema(configured)["schema"]
        note_properties = schema["properties"]["watchlist_notes"]["items"]["properties"]

        self.assertEqual(note_properties["symbol"]["enum"], ["EURUSD", "SP500"])
        self.assertEqual(note_properties["asset_class"]["enum"], ["forex", "index"])

    def test_prompt_assembly_includes_calendar_and_watchlist(self):
        processor = DailyBriefingProcessor()
        prompt_path = (
            Path(__file__).resolve().parents[2] / "prompts" / "briefing_v5.txt"
        )

        prompt = processor._build_prompt(
            template_path=str(prompt_path),
            current_date="Friday, May 08, 2026",
            macro_regime_summary="Risk-on but USD firm.",
            today_events="London 13:30 / NY 08:30 | HIGH USD | NFP",
            this_week_events="No high- or medium-impact relevant events scheduled.",
            asset_context='{"EURUSD": {"channels": ["relative monetary policy"]}}',
            watchlist=processor._format_watchlist(CONFIG),
            investment_news='[{"title":"Semiconductor capex rises"}]',
        )

        self.assertIn("Risk-on but USD firm.", prompt)
        self.assertIn("HIGH USD | NFP", prompt)
        self.assertIn("EURUSD (forex)", prompt)
        self.assertIn("UK100 (index)", prompt)
        self.assertIn("relative monetary policy", prompt)
        self.assertIn("eligibility rules, not evidence", prompt)
        self.assertIn("calculated from stored time-series observations", prompt)
        self.assertIn("Semiconductor capex rises", prompt)
        self.assertNotIn("{{investment_news}}", prompt)

    @patch("processors.briefing.get_session")
    def test_previous_briefing_excludes_old_catalysts_and_thresholds(self, get_session):
        row = Mock()
        row._mapping = {
            "sections": {
                "what_changed": "Prior change.",
                "interpretation": "Prior interpretation.",
                "invalidation": "Old 3.0% threshold.",
                "watchlist_notes": [{"next_catalyst": "Old event"}],
            }
        }
        session = Mock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value.__enter__.return_value = session

        previous = json.loads(
            DailyBriefingProcessor()._get_previous_briefing_text(CONFIG)
        )

        self.assertEqual(
            previous,
            {
                "interpretation": "Prior interpretation.",
                "what_changed": "Prior change.",
            },
        )

    @patch("processors.briefing.get_session")
    def test_regime_summary_includes_authoritative_deterministic_trends(
        self, get_session
    ):
        row = Mock()
        row._mapping = {
            "regime": "transition",
            "sub_regime": "policy_hold",
            "confidence": "moderate",
            "summary": "Policy is steady.",
            "supporting_data": {
                "deterministic_trends": [
                    {
                        "series_id": "FEDFUNDS",
                        "statement": "Federal funds rate held at 5.33%.",
                    }
                ]
            },
        }
        session = Mock()
        session.execute.return_value.fetchone.return_value = row
        get_session.return_value.__enter__.return_value = session

        summary = DailyBriefingProcessor()._get_regime_summary(CONFIG)

        self.assertIn("Deterministic Trend Signals (authoritative)", summary)
        self.assertIn("Federal funds rate held at 5.33%.", summary)

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
        sections = {
            "what_changed": "changed",
            "interpretation": "interpretation",
            "invalidation": "invalidation",
            "watchlist_notes": notes,
        }
        valid, warnings = validate_briefing_sections(sections, WATCHLIST)
        self.assertTrue(valid, warnings)

        valid, warnings = validate_briefing_sections(
            {**sections, "watchlist_notes": notes[:-1]}, WATCHLIST
        )
        self.assertFalse(valid)
        self.assertTrue(any("UK100" in warning for warning in warnings))

    def test_empty_watchlist_handled_gracefully(self):
        """validate_briefing_sections handles empty watchlist_notes without crashing."""
        valid, warnings = validate_briefing_sections({"watchlist_notes": []}, WATCHLIST)

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

    def test_configured_asset_class_overrides_model_output(self):
        sections = {
            "watchlist_notes": [
                {
                    "symbol": "DXY",
                    "asset_class": "forex",
                }
            ]
        }

        warnings = coerce_briefing_fields(sections, WATCHLIST)

        self.assertEqual(sections["watchlist_notes"][0]["asset_class"], "index")
        self.assertTrue(any("asset_class" in warning for warning in warnings))

    @patch("processors.briefing.call_llm")
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
