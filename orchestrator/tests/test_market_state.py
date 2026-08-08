import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_state

AS_OF = datetime(2026, 1, 2, 12, tzinfo=UTC)


def row(timestamp, close, *, symbol="EURUSD", high=None, low=None, open_value=None):
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": close if open_value is None else open_value,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": 1,
        "source": "test",
    }


class ReturnTests(unittest.TestCase):
    def test_return_math_and_unavailable_denominators(self):
        self.assertAlmostEqual(market_state.calculate_return(110, 100), 0.1)
        self.assertIsNone(market_state.calculate_return(110, 0))
        self.assertEqual(
            market_state.return_result(110, 0)["reason"], "zero_denominator"
        )
        self.assertEqual(
            market_state.return_result(None, 100)["reason"], "missing_data"
        )

    def test_position_and_volatility_are_finite_or_explicitly_unavailable(self):
        self.assertAlmostEqual(
            market_state.session_high_low_position(105, 110, 100), 0.5
        )
        self.assertIsNone(market_state.session_high_low_position(100, 100, 100))
        self.assertIsNotNone(market_state.realized_volatility([100, 101, 100]))
        self.assertEqual(market_state.realized_volatility([100, 100]), 0.0)


class DeterministicLabelTests(unittest.TestCase):
    def test_trend_and_session_break_labels(self):
        rows = [
            row(AS_OF - timedelta(days=1, minutes=2), 100, high=102, low=98),
            row(AS_OF - timedelta(days=1), 101, high=103, low=99),
            row(AS_OF - timedelta(minutes=1), 104, high=105, low=100),
            row(AS_OF, 104, high=106, low=101),
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "event-1", market_rows=rows, trend_window=3
        )
        self.assertEqual(snapshot["features"]["trend"]["value"], "up")
        self.assertEqual(snapshot["features"]["session_break"]["value"], "breakout_up")
        self.assertEqual(snapshot["features"]["returns"]["1m"]["value"], 0.0)

    def test_state_labels_have_stable_precedence(self):
        self.assertEqual(
            market_state.volatility_state_change(2, 1, threshold=0.1)["value"],
            "volatility_rising",
        )
        self.assertEqual(
            market_state.correlation_state_change(1, 1, threshold=0.1)["value"],
            "correlation_stable",
        )
        self.assertEqual(
            market_state.volatility_state_change(None, 1)["value"], "volatility_unknown"
        )


class SessionAndBoundTests(unittest.TestCase):
    def test_source_query_is_time_and_row_bounded_without_transaction_ownership(self):
        session = MagicMock()
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        session.execute.return_value = result
        snapshot = market_state.compute_feature_snapshot(
            session, "EURUSD", AS_OF, "event-1", row_limit=99
        )
        self.assertEqual(snapshot["unavailable"]["last"], "no_rows")
        statement, params = session.execute.call_args.args
        sql = str(statement)
        self.assertIn("timestamp >=", sql)
        self.assertIn("LIMIT", sql)
        self.assertEqual(params["row_limit"], 99)
        session.commit.assert_not_called()
        session.rollback.assert_not_called()
        session.close.assert_not_called()

    def test_snapshot_upsert_is_idempotent_sql_and_does_not_commit(self):
        session = MagicMock()
        snapshot = {
            "symbol": "EURUSD",
            "as_of": AS_OF.isoformat(),
            "source_event_id": "event-1",
            "features": {"last": {"value": 1.0, "reason": None}},
            "unavailable": {},
        }
        first = market_state.save_feature_snapshot(session, snapshot)
        second = market_state.save_feature_snapshot(session, snapshot)
        self.assertEqual(first["features"], second["features"])
        self.assertEqual(session.execute.call_count, 2)
        self.assertIn("ON CONFLICT", str(session.execute.call_args.args[0]))
        self.assertIn(
            "CAST(:features AS JSONB)", str(session.execute.call_args.args[0])
        )
        session.commit.assert_not_called()

    def test_price_event_object_reads_bounded_history_and_persists(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        source = SimpleNamespace(
            event_id="event-1",
            payload={
                "symbol": "EURUSD",
                "timestamp": AS_OF,
                "close": 1.1,
            },
        )
        snapshot = market_state.update_price_features(
            session,
            source,
            {"market_state": {"query_limit": 99}},
        )
        self.assertEqual(snapshot["source_event_id"], "event-1")
        self.assertEqual(session.execute.call_count, 2)
        self.assertIn(
            "FROM market_data", str(session.execute.call_args_list[0].args[0])
        )
        self.assertEqual(session.execute.call_args_list[0].args[1]["row_limit"], 99)
        self.assertIn(
            "INSERT INTO market_feature_snapshots",
            str(session.execute.call_args_list[1].args[0]),
        )
        session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
