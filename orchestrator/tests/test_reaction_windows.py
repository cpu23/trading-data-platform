from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from reaction_windows import (
    HORIZONS,
    _price,
    backfill_reaction_windows,
    calculate_window_metrics,
    classify_direction,
    classify_reaction_state,
    initialize_reaction_windows,
)


class _Result:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, pending=None, market=None):
        self.pending = pending or []
        self.market = list(market or [])
        self.calls = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "FROM event_reaction_windows" in sql:
            return _Result(self.pending, rowcount=0)
        if "FROM market_data" in sql:
            lower = (params or {}).get("lower")
            upper = (params or {}).get("upper")
            return _Result(
                [row for row in self.market if lower <= row["timestamp"] <= upper]
            )
        return _Result(rowcount=1)


class ReactionMathTests(unittest.TestCase):
    def test_exact_window_math_and_zero_prices(self):
        self.assertEqual(
            calculate_window_metrics(100, 102, volatility=2),
            {
                "absolute_move": 2.0,
                "percentage_move": 2.0,
                "volatility_adjusted_move": 1.0,
                "missing_data_reason": None,
            },
        )
        self.assertEqual(_price({"close": None, "open": 101}), 101.0)
        self.assertEqual(
            calculate_window_metrics(0, 102)["missing_data_reason"], "zero_baseline"
        )
        self.assertEqual(
            calculate_window_metrics(100, 0)["missing_data_reason"], "zero_target"
        )
        self.assertEqual(
            calculate_window_metrics(None, 102)["missing_data_reason"],
            "missing_baseline",
        )
        self.assertEqual(
            calculate_window_metrics(100, None)["missing_data_reason"], "missing_target"
        )

    def test_expected_direction_and_path_states(self):
        self.assertEqual(classify_direction(2, "positive", "positive"), "aligned")
        self.assertEqual(classify_direction(-2, "positive", "positive"), "opposed")
        self.assertEqual(classify_direction(0, "positive"), "neutral")
        self.assertEqual(classify_reaction_state([101, 102, 103], 100), "persistence")
        self.assertEqual(classify_reaction_state([99, 98, 101], 100), "reversal")
        self.assertEqual(classify_reaction_state([101, 99, 101], 100), "mixed")


class ReactionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.event_id = uuid4()
        self.event_at = datetime(2026, 8, 6, 12, tzinfo=UTC)
        self.config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "Consumer Price Index",
                    "instruments": ["EURUSD", "USDJPY"],
                    "priority": "high",
                    "expected_sensitivity": {
                        "EURUSD": "negative",
                        "USDJPY": "positive",
                    },
                }
            },
        }
        self.event = {
            "event_id": self.event_id,
            "observed_at": self.event_at,
            "payload": {"series_id": "CPI"},
            "source_event_id": "source-1",
        }

    def test_initialize_is_idempotent_and_creates_all_horizons(self):
        session = _Session()
        summary = initialize_reaction_windows(
            session, self.event, self.config, now=self.event_at
        )
        self.assertEqual(summary["mapped_instruments"], 2)
        self.assertEqual(summary["horizons"], len(HORIZONS))
        self.assertEqual(summary["created"], 12)
        self.assertEqual(summary["existing"], 0)
        self.assertEqual(sum("ON CONFLICT" in sql for sql, _ in session.calls), 12)

    def test_later_cycle_backfills_only_eligible_window(self):
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 7,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "1m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "down",
                "sensitivity": "negative",
                "reaction_state": "pending",
                "missing_data_reason": "future_window",
                "provenance": {"mapping_key": "CPI"},
            }
        ]
        market = [
            {"timestamp": self.event_at, "close": 100.0, "source": "oanda"},
            {"timestamp": target_at, "close": 99.0, "source": "oanda"},
            {
                "timestamp": target_at + timedelta(seconds=20),
                "close": 98.5,
                "source": "oanda",
            },
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(seconds=1)
        )
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["unresolved"], 0)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ]
        self.assertEqual(updates[-1]["percentage_move"], -1.0)
        self.assertEqual(updates[-1]["direction_vs_expected"], "aligned")
        self.assertEqual(updates[-1]["reaction_state"], "persistence")


if __name__ == "__main__":
    unittest.main()
