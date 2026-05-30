import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from processors._validators import validate_briefing_sections
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
        prompt_path = Path(__file__).resolve().parents[2] / "prompts" / "briefing_v3.txt"

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

    def test_validation_requires_all_symbols_once_in_order(self):
        notes = [
            {
                "symbol": item["symbol"],
                "asset_class": item["type"],
                "bias": "mixed",
                "confidence": "moderate",
                "summary": "summary",
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


if __name__ == "__main__":
    unittest.main()
