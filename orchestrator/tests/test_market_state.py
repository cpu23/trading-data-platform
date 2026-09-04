import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def intraday_closes(closes, *, symbol="EURUSD"):
    """Ascending 5-minute rows ending at AS_OF (all within the same day)."""
    start = AS_OF - timedelta(minutes=5 * (len(closes) - 1))
    return [
        row(start + timedelta(minutes=5 * index), close, symbol=symbol)
        for index, close in enumerate(closes)
    ]


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
            MagicMock(), "EURUSD", AS_OF, "event-1", market_rows=rows, trend_bars=3
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
            session, "EURUSD", AS_OF, "event-1", rows_per_symbol=99
        )
        self.assertEqual(snapshot["unavailable"]["last"], "no_rows")
        statement, params = session.execute.call_args.args
        sql = str(statement)
        self.assertIn("timestamp >=", sql)
        self.assertIn("ROW_NUMBER() OVER (PARTITION BY symbol", sql)
        self.assertIn("_rank <= :rows_per_symbol", sql)
        self.assertEqual(params["rows_per_symbol"], 99)
        self.assertEqual(params["start_at"], AS_OF - timedelta(days=7))
        session.commit.assert_not_called()
        session.rollback.assert_not_called()
        session.close.assert_not_called()

    def test_fetch_sql_is_per_symbol_and_filters_nonfinite_ohlc(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        market_state.compute_feature_snapshot(
            session,
            "EURUSD",
            AS_OF,
            "event-1",
            symbols=["AUDJPY"],
            rows_per_symbol=42,
        )
        sql = str(session.execute.call_args.args[0])
        params = session.execute.call_args.args[1]
        self.assertIn(
            "ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY timestamp DESC)", sql
        )
        self.assertIn(
            "open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL "
            "AND close IS NOT NULL",
            sql,
        )
        self.assertIn("'Infinity'::double precision", sql)
        self.assertEqual(params["symbols"], ["EURUSD", "AUDJPY"])
        self.assertEqual(params["rows_per_symbol"], 42)

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
            {"market_state": {"rows_per_symbol": 99}},
        )
        self.assertEqual(snapshot["source_event_id"], "event-1")
        self.assertEqual(session.execute.call_count, 2)
        self.assertIn(
            "FROM market_data", str(session.execute.call_args_list[0].args[0])
        )
        self.assertEqual(
            session.execute.call_args_list[0].args[1]["rows_per_symbol"], 99
        )
        self.assertIn(
            "INSERT INTO market_feature_snapshots",
            str(session.execute.call_args_list[1].args[0]),
        )
        session.commit.assert_not_called()


class RuntimeFieldFlowTests(unittest.TestCase):
    """Every documented market-state field must flow into the computation."""

    def test_every_documented_field_flows_into_computation(self):
        session = MagicMock()
        source = SimpleNamespace(
            event_id="event-1",
            payload={
                "symbol": "EURUSD",
                "timestamp": AS_OF,
                "yields": {"DGS10": 4.2, "DGS2": 2.5},
            },
        )
        config = {
            "market_state": {
                "enabled": True,
                "rows_per_symbol": 77,
                "snapshot_limit": 33,
                "trend_bars": 5,
                "zscore_bars": 9,
                "volatility_bars": 8,
                "lookback": {"value": 2, "unit": "hours"},
                "state_thresholds": {
                    "trend_slope_epsilon": 0.25,
                    "high_volatility_threshold": 0.5,
                    "high_correlation_threshold": 0.6,
                },
                "baskets": {"risk": ["EURUSD", "AUDJPY"]},
                "yield_curves": {"us_10y_2y": ["DGS10", "DGS2"]},
            }
        }
        with patch(
            "market_state.compute_feature_snapshot",
            return_value={
                "symbol": "EURUSD",
                "as_of": AS_OF.isoformat(),
                "source_event_id": "event-1",
                "features": {},
                "unavailable": {},
                "provenance": {},
            },
        ) as compute:
            market_state.update_price_features(session, source, config)
        kwargs = compute.call_args.kwargs
        self.assertEqual(kwargs["rows_per_symbol"], 77)
        self.assertEqual(kwargs["trend_bars"], 5)
        self.assertEqual(kwargs["zscore_bars"], 9)
        self.assertEqual(kwargs["volatility_bars"], 8)
        self.assertEqual(kwargs["lookback"], timedelta(hours=2))
        self.assertEqual(kwargs["trend_slope_epsilon"], 0.25)
        self.assertEqual(kwargs["high_volatility_threshold"], 0.5)
        self.assertEqual(kwargs["high_correlation_threshold"], 0.6)
        self.assertEqual(kwargs["baskets"], {"risk": ["EURUSD", "AUDJPY"]})
        self.assertEqual(kwargs["yield_curves"], {"us_10y_2y": ["DGS10", "DGS2"]})
        self.assertEqual(kwargs["symbols"], ["EURUSD", "AUDJPY"])
        self.assertEqual(kwargs["yield_observations"], {"DGS10": 4.2, "DGS2": 2.5})

    def test_lookback_units_drive_sql_window(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        source = SimpleNamespace(
            event_id="event-1", payload={"symbol": "EURUSD", "timestamp": AS_OF}
        )
        market_state.update_price_features(
            session,
            source,
            {"market_state": {"lookback": {"value": 2, "unit": "hours"}}},
        )
        params = session.execute.call_args_list[0].args[1]
        self.assertEqual(params["start_at"], AS_OF - timedelta(hours=2))

    def test_snapshot_limit_sets_listing_default(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        market_state.list_market_features(
            session, config={"market_state": {"snapshot_limit": 33}}
        )
        params = session.execute.call_args.args[1]
        self.assertEqual(params["row_limit"], 33)


class RuntimeFieldBehaviorTests(unittest.TestCase):
    """Each supported field changes computed behavior, not just parameters."""

    def test_trend_bars_change_trend_label(self):
        rows = [
            row(AS_OF - timedelta(days=5), 100),
            row(AS_OF - timedelta(days=4), 102),
            row(AS_OF - timedelta(days=3), 104),
            row(AS_OF - timedelta(days=2), 106),
            row(AS_OF - timedelta(days=1), 107),
            row(AS_OF, 107),
        ]
        short = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows, trend_bars=2
        )
        long = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows, trend_bars=4
        )
        self.assertEqual(short["features"]["trend"]["value"], "flat")
        self.assertEqual(long["features"]["trend"]["value"], "up")

    def test_trend_slope_epsilon_changes_trend_label(self):
        rows = [
            row(AS_OF - timedelta(days=2), 100.0),
            row(AS_OF - timedelta(days=1), 100.001),
            row(AS_OF, 100.002),
        ]
        loose = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=rows,
            trend_slope_epsilon=0.005,
        )
        tight = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=rows,
            trend_slope_epsilon=0.0001,
        )
        self.assertEqual(loose["features"]["trend"]["value"], "flat")
        self.assertEqual(tight["features"]["trend"]["value"], "up")

    def test_zscore_bars_change_intraday_zscore(self):
        rows = intraday_closes([100, 101, 100, 102, 101, 103])
        windowed = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows, zscore_bars=2
        )
        full = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows
        )
        self.assertAlmostEqual(windowed["features"]["intraday_zscore"]["value"], 1.0)
        self.assertNotEqual(
            full["features"]["intraday_zscore"]["value"],
            windowed["features"]["intraday_zscore"]["value"],
        )

    def test_volatility_bars_slice_exactly(self):
        rows = intraday_closes([100, 100, 100, 100, 100, 100, 100, 101, 102, 103])
        sliced = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows, volatility_bars=3
        )
        wide = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows, volatility_bars=10
        )
        expected = market_state.realized_volatility([101, 102, 103])
        self.assertAlmostEqual(
            sliced["features"]["realized_volatility"]["value"], expected
        )
        self.assertNotEqual(
            wide["features"]["realized_volatility"]["value"],
            sliced["features"]["realized_volatility"]["value"],
        )

    def test_high_volatility_threshold_changes_level_label(self):
        rows = intraday_closes([100, 101, 102])
        sensitive = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=rows,
            high_volatility_threshold=0.01,
        )
        tolerant = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=rows,
            high_volatility_threshold=0.1,
        )
        self.assertEqual(
            sensitive["features"]["volatility_level"]["value"], "volatility_high"
        )
        self.assertEqual(
            tolerant["features"]["volatility_level"]["value"], "volatility_normal"
        )
        self.assertAlmostEqual(
            tolerant["features"]["volatility_level"]["volatility"],
            market_state.realized_volatility([100, 101, 102]),
            places=6,
        )

    def test_update_price_features_threshold_changes_output_without_previous_volatility(
        self,
    ):
        # End to end: a plain price_tick (no previous_volatility in the payload)
        # still consumes high_volatility_threshold through the always-present
        # volatility_level classification.
        rows = intraday_closes([100, 101, 102])
        base = {"symbol": "EURUSD", "timestamp": AS_OF}
        for threshold, expected in (
            (0.01, "volatility_high"),
            (0.1, "volatility_normal"),
        ):
            session = MagicMock()
            session.execute.return_value.mappings.return_value.all.return_value = rows
            source = SimpleNamespace(event_id="event-1", payload=dict(base))
            snapshot = market_state.update_price_features(
                session,
                source,
                {
                    "market_state": {
                        "state_thresholds": {"high_volatility_threshold": threshold}
                    }
                },
            )
            self.assertEqual(
                snapshot["features"]["volatility_level"]["value"], expected
            )

    def test_high_correlation_threshold_classifies_cross_asset_pairs(self):
        # Close-to-close returns: A -> [1,2,3,4,5], B -> [1,2,0.5,4,1].
        # Pearson on the five aligned return pairs is 0.2265.
        timestamps = [AS_OF - timedelta(minutes=10 - index) for index in range(6)]
        a_closes = [100, 200, 600, 2400, 12000, 72000]
        b_closes = [100, 200, 600, 900, 4500, 9000]
        rows = [
            row(ts, close, symbol="EURUSD")
            for ts, close in zip(timestamps, a_closes, strict=False)
        ] + [
            row(ts, close, symbol="AUDJPY")
            for ts, close in zip(timestamps, b_closes, strict=False)
        ]
        strict = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY"],
            market_rows=rows,
            high_correlation_threshold=0.5,
        )
        lenient = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY"],
            market_rows=rows,
            high_correlation_threshold=0.2,
        )
        pair = "AUDJPY:EURUSD"
        self.assertEqual(
            strict["features"]["correlations"][pair]["value"], "correlation_normal"
        )
        self.assertEqual(
            lenient["features"]["correlations"][pair]["value"], "correlation_high"
        )
        self.assertAlmostEqual(
            strict["features"]["correlations"][pair]["correlation"], 0.2265, places=3
        )
        self.assertEqual(strict["features"]["correlations"][pair]["pairs"], 5)

    def test_returns_alignment_ignores_shifted_and_missing_timestamps(self):
        timestamps = [AS_OF - timedelta(minutes=10 - index) for index in range(6)]
        a_closes = [100, 200, 600, 2400, 12000, 72000]
        # B is missing the first observation: returns exist only at t2..t5.
        b_missing = [200, 600, 900, 4500, 9000]
        rows_missing = [
            row(ts, close, symbol="EURUSD")
            for ts, close in zip(timestamps, a_closes, strict=False)
        ] + [
            row(ts, close, symbol="AUDJPY")
            for ts, close in zip(timestamps[1:], b_missing, strict=False)
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY"],
            market_rows=rows_missing,
            high_correlation_threshold=0.2,
        )
        pair = "AUDJPY:EURUSD"
        self.assertEqual(snapshot["features"]["correlations"][pair]["pairs"], 4)
        self.assertEqual(
            snapshot["features"]["correlations"][pair]["reason"],
            "insufficient_paired_samples",
        )
        # B shifted by half a bar: no shared return timestamps at all.
        b_closes = [100, 200, 600, 900, 4500, 9000]
        rows_shifted = [
            row(ts, close, symbol="EURUSD")
            for ts, close in zip(timestamps, a_closes, strict=False)
        ] + [
            row(ts + timedelta(seconds=30), close, symbol="AUDJPY")
            for ts, close in zip(timestamps, b_closes, strict=False)
        ]
        shifted = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY"],
            market_rows=rows_shifted,
            high_correlation_threshold=0.2,
        )
        self.assertEqual(shifted["features"]["correlations"][pair]["pairs"], 0)
        self.assertEqual(
            shifted["features"]["correlations"][pair]["reason"],
            "insufficient_paired_samples",
        )

    def test_common_trend_but_uncorrelated_returns_is_not_high(self):
        # Both symbols trend up in LEVELS (spurious high level correlation)
        # but their close-to-close returns are effectively uncorrelated.
        timestamps = [AS_OF - timedelta(minutes=10 - index) for index in range(6)]
        a_closes = [100, 101, 102, 103, 104, 105]
        b_closes = [100, 102, 101, 104, 103, 106]
        rows = [
            row(ts, close, symbol="EURUSD")
            for ts, close in zip(timestamps, a_closes, strict=False)
        ] + [
            row(ts, close, symbol="AUDJPY")
            for ts, close in zip(timestamps, b_closes, strict=False)
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY"],
            market_rows=rows,
            high_correlation_threshold=0.3,
        )
        pair = "AUDJPY:EURUSD"
        metric = snapshot["features"]["correlations"][pair]
        self.assertEqual(metric["value"], "correlation_normal")
        self.assertLess(abs(metric["correlation"]), 0.3)
        # Contrast: position-paired raw levels are spuriously high.
        self.assertGreater(market_state.correlation(a_closes, b_closes), 0.8)


class DefinitionSeparationTests(unittest.TestCase):
    """Basket/yield-curve definitions must never masquerade as observations."""

    def test_basket_definitions_select_observation_members(self):
        rows = [
            row(AS_OF - timedelta(minutes=2), 100, symbol="EURUSD"),
            row(AS_OF - timedelta(minutes=1), 101, symbol="EURUSD"),
            row(AS_OF - timedelta(minutes=2), 90, symbol="AUDJPY"),
            row(AS_OF - timedelta(minutes=1), 89, symbol="AUDJPY"),
            row(AS_OF - timedelta(minutes=2), 10, symbol="XAUUSD"),
            row(AS_OF - timedelta(minutes=1), 10.5, symbol="XAUUSD"),
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY", "XAUUSD"],
            market_rows=rows,
            baskets={"risk": ["EURUSD", "AUDJPY"]},
        )
        breadth = snapshot["features"]["basket_breadth"]["risk"]
        self.assertEqual(breadth["total"], 2)
        self.assertEqual(breadth["advancing"], 1)
        self.assertEqual(breadth["declining"], 1)
        self.assertEqual(breadth["value"], 0.5)

    def test_basket_definitions_without_observations_report_missing_data(self):
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=[],
            baskets={"risk": ["EURUSD", "AUDJPY"]},
        )
        self.assertEqual(
            snapshot["features"]["basket_breadth"]["risk"]["reason"], "missing_data"
        )

    def test_yield_definitions_select_observation_keys(self):
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=[],
            yield_curves={"us_10y_2y": ["DGS10", "DGS2"]},
            yield_observations={"DGS10": 4.2, "DGS2": 2.5},
        )
        spread = snapshot["features"]["yield_curve_spreads"]["us_10y_2y"]
        self.assertAlmostEqual(spread["value"], 1.7)

    def test_yield_definitions_without_observations_report_missing_data(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        snapshot = market_state.compute_feature_snapshot(
            session,
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=[],
            yield_curves={"us_10y_2y": ["DGS10", "DGS2"]},
        )
        spread = snapshot["features"]["yield_curve_spreads"]["us_10y_2y"]
        self.assertEqual(spread["reason"], "missing_data")
        self.assertEqual(
            snapshot["unavailable"]["yield_curve_spreads.us_10y_2y"], "missing_data"
        )

    def test_yield_observations_fetched_from_macro_series_end_to_end(self):
        # Plain price_tick without payload yields: observations come from
        # macro_series (latest finite value per configured key, at/before as_of).
        def result_for(rows):
            result = MagicMock()
            result.mappings.return_value.all.return_value = rows
            return result

        yield_rows = [
            {
                "series_id": "DGS10",
                "observed_at": AS_OF - timedelta(hours=1),
                "value": 4.2,
            },
            {
                "series_id": "DGS2",
                "observed_at": AS_OF - timedelta(hours=1),
                "value": 2.5,
            },
        ]
        session = MagicMock()
        session.execute.side_effect = [
            result_for([]),  # market_data price fetch
            result_for(yield_rows),  # macro_series yield fetch
            result_for([]),  # snapshot save (result unused)
        ]
        source = SimpleNamespace(
            event_id="event-1",
            payload={"symbol": "EURUSD", "timestamp": AS_OF},
        )
        snapshot = market_state.update_price_features(
            session,
            source,
            {
                "market_state": {
                    "yield_curves": {"us_10y_2y": ["DGS10", "DGS2"]},
                }
            },
        )
        spread = snapshot["features"]["yield_curve_spreads"]["us_10y_2y"]
        self.assertAlmostEqual(spread["value"], 1.7)
        yield_statement, yield_params = session.execute.call_args_list[1].args
        sql = str(yield_statement)
        self.assertIn("FROM macro_series", sql)
        self.assertIn(
            "ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY observed_at DESC)",
            sql,
        )
        self.assertEqual(yield_params["series_ids"], ["DGS10", "DGS2"])
        self.assertEqual(yield_params["as_of"], AS_OF)
        self.assertEqual(yield_params["rows_per_key"], 1)

    def test_payload_yields_override_macro_series_fetch(self):
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []
        source = SimpleNamespace(
            event_id="event-1",
            payload={
                "symbol": "EURUSD",
                "timestamp": AS_OF,
                "yields": {"DGS10": 4.2, "DGS2": 2.5},
            },
        )
        snapshot = market_state.update_price_features(
            session,
            source,
            {
                "market_state": {
                    "yield_curves": {"us_10y_2y": ["DGS10", "DGS2"]},
                }
            },
        )
        spread = snapshot["features"]["yield_curve_spreads"]["us_10y_2y"]
        self.assertAlmostEqual(spread["value"], 1.7)
        # Only the price fetch and the save execute; no macro_series query.
        self.assertEqual(session.execute.call_count, 2)
        for call in session.execute.call_args_list:
            self.assertNotIn("macro_series", str(call.args[0]))


class OHLCOutputQualityTests(unittest.TestCase):
    def test_null_and_nonfinite_ohlc_rows_are_filtered(self):
        rows = [
            row(AS_OF - timedelta(minutes=1), 100, high=float("nan")),
            row(AS_OF, 101, high=103, low=99),
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows
        )
        self.assertEqual(snapshot["features"]["last"]["value"], 101)
        self.assertEqual(
            snapshot["features"]["session_high_low_position"]["value"], 0.5
        )

    def test_infinite_ohlc_rows_are_filtered(self):
        rows = [
            row(AS_OF - timedelta(days=1), 100, open_value=float("inf")),
            row(AS_OF, 101),
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows
        )
        self.assertEqual(snapshot["features"]["last"]["value"], 101)
        self.assertEqual(
            snapshot["features"]["trend"]["reason"], "insufficient_history"
        )

    def test_incomplete_ohlc_only_rows_report_no_rows(self):
        rows = [row(AS_OF - timedelta(minutes=1), None), row(AS_OF, None)]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows
        )
        self.assertEqual(snapshot["unavailable"]["last"], "no_rows")

    def test_finite_metrics_carry_no_missing_data_reason(self):
        rows = intraday_closes([100, 101, 102, 103, 104])
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(), "EURUSD", AS_OF, "e1", market_rows=rows
        )
        features = snapshot["features"]
        self.assertIsNotNone(features["realized_volatility"]["value"])
        self.assertIsNone(features["realized_volatility"]["reason"])
        self.assertIsNotNone(features["intraday_zscore"]["value"])
        self.assertIsNone(features["intraday_zscore"]["reason"])
        self.assertIsNotNone(features["session_high_low_position"]["value"])
        self.assertIsNone(features["session_high_low_position"]["reason"])
        self.assertNotIn("realized_volatility", snapshot["unavailable"])
        self.assertNotIn("intraday_zscore", snapshot["unavailable"])

    def test_missing_metrics_still_carry_reasons(self):
        # A one-bar volatility slice and constant z-score values must keep
        # their explicit missing-data reasons.
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            market_rows=intraday_closes([100, 100]),
            volatility_bars=1,
        )
        features = snapshot["features"]
        self.assertEqual(
            features["realized_volatility"]["reason"], "insufficient_history"
        )
        self.assertEqual(
            features["intraday_zscore"]["reason"], "insufficient_or_constant_data"
        )

    def test_dense_symbol_rows_do_not_starve_other_symbols(self):
        dense = [
            row(AS_OF - timedelta(minutes=index), 100 + (index % 3), symbol="EURUSD")
            for index in range(5000)
        ]
        sparse = [
            row(AS_OF - timedelta(minutes=2), 90, symbol="AUDJPY"),
            row(AS_OF - timedelta(minutes=1), 91, symbol="AUDJPY"),
        ]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "e1",
            symbols=["AUDJPY"],
            market_rows=[*dense, *sparse],
            baskets={"risk": ["EURUSD", "AUDJPY"]},
        )
        breadth = snapshot["features"]["basket_breadth"]["risk"]
        self.assertEqual(breadth["total"], 2)
        self.assertEqual(breadth["advancing"], 1)
        self.assertEqual(breadth["declining"], 1)


class ProvenanceTests(unittest.TestCase):
    def test_snapshot_carries_market_state_v2_provenance(self):
        rows = [row(AS_OF - timedelta(days=1), 100), row(AS_OF, 101)]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "event-1",
            market_rows=rows,
            lookback=timedelta(days=3),
            rows_per_symbol=99,
        )
        provenance = snapshot["provenance"]
        self.assertEqual(provenance["version"], "market-state-v2")
        self.assertEqual(provenance["source_table"], "market_data")
        self.assertEqual(provenance["lookback_seconds"], 3 * 86400)
        self.assertEqual(provenance["rows_per_symbol"], 99)
        self.assertIs(provenance["ohlc_filtered"], True)
        self.assertEqual(provenance["symbols_requested"], 1)
        self.assertEqual(
            provenance["correlation_method"],
            "close_to_close_returns_on_shared_timestamps",
        )
        self.assertEqual(provenance["correlation_min_pairs"], 5)
        self.assertEqual(provenance["yield_source"], "macro_series")
        self.assertEqual(provenance["yield_rows_per_key"], 1)

    def test_save_persists_provenance_inside_features(self):
        session = MagicMock()
        snapshot = {
            "symbol": "EURUSD",
            "as_of": AS_OF.isoformat(),
            "source_event_id": "event-1",
            "features": {"last": {"value": 1.0, "reason": None}},
            "unavailable": {},
            "provenance": {"rows_per_symbol": 99},
        }
        returned = market_state.save_feature_snapshot(session, snapshot)
        params = session.execute.call_args.args[1]
        persisted = json.loads(params["features"])
        self.assertEqual(persisted["provenance"]["version"], "market-state-v2")
        self.assertEqual(persisted["provenance"]["rows_per_symbol"], 99)
        self.assertEqual(persisted["last"], {"value": 1.0, "reason": None})
        self.assertIn("provenance", returned["features"])

    def test_config_flags_surface_in_provenance(self):
        rows = [row(AS_OF - timedelta(days=1), 100), row(AS_OF, 101)]
        snapshot = market_state.compute_feature_snapshot(
            MagicMock(),
            "EURUSD",
            AS_OF,
            "event-1",
            market_rows=rows,
            config_issues=[
                "market_state.zscore_bars is documented but has no consumer"
            ],
        )
        self.assertEqual(
            snapshot["provenance"]["config_flags"],
            ["market_state.zscore_bars is documented but has no consumer"],
        )


class ConfigValidationTests(unittest.TestCase):
    def test_unknown_or_misspelled_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            market_state.validate_market_state_config({"treand_window": 5})
        with self.assertRaises(ValueError):
            market_state.validate_market_state_config({"query_limit": 500})
        with self.assertRaises(ValueError):
            market_state.validate_market_state_config(
                {"realized_volatility_window": 30}
            )

    def test_duplicate_yield_keys_are_rejected_at_validation(self):
        # A 3-item list with a duplicate passes a set-size check but would be
        # rejected at runtime; the model must reject it at startup.
        with self.assertRaises(ValueError):
            market_state.validate_market_state_config(
                {"yield_curves": {"us_10y_2y": ["DGS10", "DGS2", "DGS10"]}}
            )
        with self.assertRaises(ValueError):
            market_state.validate_market_state_config(
                {"yield_curves": {"us_10y_2y": ["DGS10"]}}
            )
        with self.assertRaises(ValueError):
            market_state.validate_market_state_config(
                {"yield_curves": {"us_10y_2y": ["DGS10", "DGS2", "DGS30"]}}
            )
        validated, issues = market_state.validate_market_state_config(
            {"yield_curves": {"us_10y_2y": ["DGS10", "DGS2"]}}
        )
        self.assertEqual(issues, [])

    def test_update_price_features_rejects_unknown_fields(self):
        session = MagicMock()
        source = SimpleNamespace(
            event_id="event-1", payload={"symbol": "EURUSD", "timestamp": AS_OF}
        )
        with self.assertRaises(ValueError):
            market_state.update_price_features(
                session, source, {"market_state": {"treand_window": 5}}
            )

    def test_documented_field_with_no_consumer_is_flagged(self):
        consumers = {
            key: value
            for key, value in market_state.MARKET_STATE_CONSUMERS.items()
            if key != "trend_bars"
        }
        validated, issues = market_state.validate_market_state_config(
            {"trend_bars": 20}, consumers=consumers
        )
        self.assertEqual(
            issues, ["market_state.trend_bars is documented but has no consumer"]
        )

    def test_documented_threshold_with_no_consumer_is_flagged(self):
        consumers = {
            key: value
            for key, value in market_state.MARKET_STATE_CONSUMERS.items()
            if key != "state_thresholds.trend_slope_epsilon"
        }
        validated, issues = market_state.validate_market_state_config(
            {"state_thresholds": {"trend_slope_epsilon": 0.1}}, consumers=consumers
        )
        self.assertEqual(
            issues,
            [
                "market_state.state_thresholds.trend_slope_epsilon is documented "
                "but has no consumer"
            ],
        )

    def test_consumer_registry_covers_every_model_field(self):
        validated, issues = market_state.validate_market_state_config({})
        self.assertEqual(issues, [])
        model_fields = set(type(validated).model_fields)
        top_level = {
            key for key in market_state.MARKET_STATE_CONSUMERS if "." not in key
        }
        self.assertEqual(
            model_fields,
            {
                "enabled",
                "rows_per_symbol",
                "snapshot_limit",
                "trend_bars",
                "zscore_bars",
                "volatility_bars",
                "lookback",
                "state_thresholds",
                "baskets",
                "yield_curves",
            },
        )
        self.assertEqual(model_fields, top_level)
        nested = {key for key in market_state.MARKET_STATE_CONSUMERS if "." in key}
        self.assertEqual(
            nested,
            {
                f"state_thresholds.{name}"
                for name in type(validated.state_thresholds).model_fields
            },
        )


if __name__ == "__main__":
    unittest.main()
