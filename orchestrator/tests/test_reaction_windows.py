from __future__ import annotations

import json
import math
import unittest
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

from reaction_windows import (
    HORIZONS,
    _price,
    _realized_volatility,
    backfill_reaction_windows,
    calculate_window_metrics,
    classify_direction,
    classify_reaction_state,
    horizon_target,
    initialize_reaction_windows,
    recompute_reaction_windows,
)
from venue_calendar import venue_for_symbol


class _Result:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


def _usable(row):
    """Emulate the SQL finite-price predicate: usable when the close is
    finite, or the close is NULL and the open is finite."""
    close = row.get("close")
    if close is not None:
        return isinstance(close, (int, float)) and math.isfinite(close)
    open_value = row.get("open")
    return open_value is not None and math.isfinite(open_value)


class _Session:
    """Fake session emulating the directional SQL shapes used by
    reaction_windows: baseline (DESC LIMIT 1, strictly before event_at),
    target (ASC LIMIT 1, at/after target_at within upper), pre/path bounded
    ranges, and the timeframe-scoped INSERT conflict target."""

    def __init__(self, pending=None, market=None):
        self.pending = pending or []
        self.market = [row for row in (market or []) if _usable(row)]
        self.inserted = set()
        self.calls = []

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.calls.append((sql, params))
        if "INSERT INTO event_reaction_windows" in sql:
            key = (
                params["event_id"],
                params["instrument_symbol"],
                params["timeframe"],
                params["horizon"],
            )
            if key in self.inserted:
                return _Result(rowcount=0)
            self.inserted.add(key)
            return _Result(rowcount=1)
        if "FROM event_reaction_windows" in sql:
            pending = self.pending
            if "volatility_version IS NULL" in sql:
                pending = [
                    row
                    for row in pending
                    if row.get("volatility_version") is None
                    or row.get("volatility_version", 0) < 2
                ]
            return _Result(pending, rowcount=0)
        if "FROM market_data" in sql:
            rows = self.market
            if "first_sign" in sql or "WITH path AS" in sql:
                # Post-event path classification aggregate over the full
                # window (no row cap).
                baseline = params["baseline"]
                candidates = [
                    row
                    for row in rows
                    if params["lower"] <= row["timestamp"] <= params["upper"]
                ]
                signs = []
                for row in candidates:
                    price = row.get("close")
                    if price is None:
                        price = row.get("open")
                    if price is None:
                        continue
                    if price > baseline:
                        signs.append(1)
                    elif price < baseline:
                        signs.append(-1)
                return _Result(
                    [
                        {
                            "first_sign": signs[0] if signs else None,
                            "last_sign": signs[-1] if signs else None,
                            "has_positive": 1 in signs,
                            "has_negative": -1 in signs,
                        }
                    ]
                )
            if "ORDER BY timestamp DESC LIMIT 1" in sql:
                if "event_at" in params:
                    # Direct latest pre-event row: no range bound.
                    candidates = [
                        row for row in rows if row["timestamp"] < params["event_at"]
                    ]
                else:
                    # end_of_session: final at-or-before close within tolerance.
                    candidates = [
                        row
                        for row in rows
                        if params["lower"] <= row["timestamp"] <= params["target_at"]
                    ]
                candidates.sort(key=lambda row: row["timestamp"], reverse=True)
                return _Result(candidates[:1])
            if "bucket_seconds" in params:
                # Pre-event volatility path: bucket-last downsampling across
                # the full lookback (one last sample per time bucket).
                bucket_seconds = params["bucket_seconds"]
                lower = params["lower"]
                event_at = params["event_at"]
                candidates = [
                    row for row in rows if lower <= row["timestamp"] < event_at
                ]
                best: dict[int, dict] = {}
                for row in candidates:
                    bucket = int(row["timestamp"].timestamp() // bucket_seconds)
                    if (
                        bucket not in best
                        or row["timestamp"] > best[bucket]["timestamp"]
                    ):
                        best[bucket] = row
                return _Result(sorted(best.values(), key=lambda row: row["timestamp"]))
            if "ORDER BY timestamp ASC LIMIT 1" in sql:
                target_at = params["target_at"]
                upper = params["upper"]
                candidates = [
                    row for row in rows if target_at <= row["timestamp"] <= upper
                ]
                candidates.sort(key=lambda row: row["timestamp"])
                return _Result(candidates[:1])
            selected = rows
            if params.get("lower") is not None:
                selected = [
                    row for row in selected if params["lower"] <= row["timestamp"]
                ]
            if "event_at" in params:
                selected = [
                    row for row in selected if row["timestamp"] < params["event_at"]
                ]
            if "upper" in params:
                selected = [
                    row for row in selected if row["timestamp"] <= params["upper"]
                ]
            selected.sort(key=lambda row: row["timestamp"])
            return _Result(selected)
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
        self.event_at = datetime(2026, 8, 6, 12, tzinfo=UTC)  # Thursday
        self.config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "target_tolerance_minutes": 5,
            },
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
        inserts = [
            (sql, params)
            for sql, params in session.calls
            if "INSERT INTO event_reaction_windows" in sql
        ]
        self.assertEqual(len(inserts), 12)
        self.assertTrue(
            all(
                "ON CONFLICT (event_id,instrument_symbol,timeframe,horizon)" in sql
                for sql, _ in inserts
            )
        )
        self.assertTrue(
            all(params["calendar_name"] == "fx_24x5" for _, params in inserts)
        )
        self.assertTrue(
            all(params["baseline_offset_seconds"] is None for _, params in inserts)
        )
        self.assertTrue(
            all(
                params["missing_data_reason"] == "future_window"
                for _, params in inserts
            )
        )
        # A second cycle must conflict on the timeframe-scoped identity.
        again = initialize_reaction_windows(
            session, self.event, self.config, now=self.event_at
        )
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["existing"], 12)

    def test_initialize_persists_calendar_aware_baseline_offset(self):
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=30),
                "close": 100.0,
                "source": "oanda",
            }
        ]
        session = _Session(market=market)
        initialize_reaction_windows(session, self.event, self.config, now=self.event_at)
        inserts = [
            params
            for sql, params in session.calls
            if "INSERT INTO event_reaction_windows" in sql
        ]
        baseline_params = [
            params
            for sql, params in session.calls
            if "FROM market_data" in sql and "ORDER BY timestamp DESC" in sql
        ]
        self.assertEqual(len(baseline_params), 2)  # one per mapped instrument
        self.assertEqual(
            inserts[0]["baseline_at"], self.event_at - timedelta(minutes=30)
        )
        self.assertEqual(inserts[0]["baseline_offset_seconds"], -1800)
        self.assertEqual(inserts[0]["calendar_version"], "1")
        calendar_provenance = json.loads(inserts[0]["provenance"])["calendar"]
        self.assertEqual(calendar_provenance["venue"], "fx_24x5")
        self.assertEqual(calendar_provenance["session_model"], "weekly")
        self.assertEqual(calendar_provenance["session_week"]["close_day"], "Friday")
        self.assertEqual(calendar_provenance["price_timeframe"], "PRICE")
        self.assertEqual(calendar_provenance["target_selection_policy"], "first")
        self.assertEqual(
            calendar_provenance["closure_policy"]["one_off_closures_audited_through"],
            2026,
        )

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
            # Pre-event sample strictly before the event.
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            },
            # Exact-event row must NOT be selected as baseline.
            {"timestamp": self.event_at, "close": 101.0, "source": "oanda"},
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
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[-1]["baseline_at"], self.event_at - timedelta(minutes=1)
        )
        self.assertEqual(updates[-1]["baseline_offset_seconds"], -60)
        # Target offset is relative to target_at; observed exactly at target.
        self.assertEqual(updates[-1]["target_offset_seconds"], 0)
        self.assertEqual(updates[-1]["percentage_move"], -1.0)
        self.assertEqual(updates[-1]["direction_vs_expected"], "aligned")
        # The exact-event bar (101) plus the falling target path makes the
        # post-event path reversal relative to the 11:59 baseline (100).
        self.assertEqual(updates[-1]["reaction_state"], "reversal")
        self.assertEqual(updates[-1]["target_price"], 99.0)
        self.assertEqual(updates[-1]["missing_data_reason"], None)
        self.assertEqual(updates[-1]["calendar_name"], "fx_24x5")
        self.assertEqual(updates[-1]["volatility_version"], 2)
        provenance = json.loads(updates[-1]["provenance"])
        self.assertEqual(provenance["calendar"]["venue"], "fx_24x5")
        self.assertEqual(provenance["volatility"]["version"], 2)

    def test_dense_pre_post_selection_and_exact_event_exclusion(self):
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 8,
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
                "expected_direction": "up",
                "sensitivity": "positive",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = []
        # Dense pre-event ticks at 10-second spacing (last at 11:59:50).
        for index in range(12):
            ts = (
                self.event_at
                - timedelta(minutes=1, seconds=50)
                + timedelta(seconds=10 * index)
            )
            market.append(
                {"timestamp": ts, "close": 100.0 + index * 0.1, "source": "oanda"}
            )
        # Exact-event row: must be excluded from baseline selection.
        market.append({"timestamp": self.event_at, "close": 999.0, "source": "oanda"})
        # Dense post-event ticks; first at/after target_at is 12:01:00.
        for index in range(10):
            ts = target_at + timedelta(seconds=10 * index)
            market.append(
                {"timestamp": ts, "close": 105.0 + index * 0.1, "source": "oanda"}
            )
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        # Baseline is the last tick strictly before the event, not the 999 row.
        self.assertEqual(updates["baseline_at"], self.event_at - timedelta(seconds=10))
        self.assertEqual(updates["baseline_price"], 101.0)
        self.assertEqual(updates["observed_at"], target_at)
        self.assertEqual(updates["target_price"], 105.0)
        self.assertEqual(updates["baseline_offset_seconds"], -10)
        self.assertEqual(updates["target_offset_seconds"], 0)

    def test_tolerance_boundary_includes_exact_upper(self):
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 9,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            },
            # Exactly at the tolerance bound (target_at + 5 minutes).
            {
                "timestamp": target_at + timedelta(minutes=5),
                "close": 101.0,
                "source": "oanda",
            },
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=10)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["target_price"], 101.0)
        self.assertEqual(updates["observed_at"], target_at + timedelta(minutes=5))
        self.assertEqual(updates["target_offset_seconds"], 300)
        self.assertIsNone(updates["missing_data_reason"])

    def test_tolerance_boundary_excludes_beyond_upper(self):
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 10,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            },
            # One second beyond the tolerance bound: out of scope.
            {
                "timestamp": target_at + timedelta(minutes=5, seconds=1),
                "close": 101.0,
                "source": "oanda",
            },
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=10)
        )
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(summary["unresolved"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["missing_data_reason"], "missing_target")
        self.assertIsNone(updates["observed_price"])

    def test_multi_timeframe_identity(self):
        config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "target_tolerance_minutes": 5,
            },
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "Consumer Price Index",
                    "instruments": [
                        {"symbol": "EURUSD", "timeframe": "PRICE"},
                        {"symbol": "EURUSD", "timeframe": "5m"},
                    ],
                    "expected_sensitivity": {"EURUSD": "negative"},
                }
            },
        }
        session = _Session()
        summary = initialize_reaction_windows(
            session, self.event, config, now=self.event_at
        )
        self.assertEqual(summary["mapped_instruments"], 2)
        self.assertEqual(summary["created"], 12)
        self.assertEqual(summary["existing"], 0)
        inserts = [
            params
            for sql, params in session.calls
            if "INSERT INTO event_reaction_windows" in sql
        ]
        self.assertEqual(
            sum(1 for params in inserts if params["timeframe"] == "PRICE"), 6
        )
        self.assertEqual(sum(1 for params in inserts if params["timeframe"] == "5m"), 6)
        # Distinct timeframes share no identity: a repeat cycle conflicts only
        # on the full (event_id, symbol, timeframe, horizon) key.
        again = initialize_reaction_windows(
            session, self.event, config, now=self.event_at
        )
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["existing"], 12)

    def _matched_horizon_market(self, event_at):
        """120 one-minute samples whose timestamp-paired 5-minute returns
        alternate between +0.1% and +0.2% (population std of the returns is
        the population std of [0.1, 0.2, ...])."""
        rows = []
        returns = [0.001 if bucket % 2 == 0 else 0.002 for bucket in range(24)]
        price = 100.0
        for minute in range(120):
            if minute > 0 and minute % 5 == 0:
                price = price * (1.0 + returns[minute // 5 - 1])
            ts = event_at - timedelta(minutes=120) + timedelta(minutes=minute)
            rows.append({"timestamp": ts, "close": price, "source": "oanda"})
        return rows

    def test_matched_horizon_volatility_samples(self):
        target_at = self.event_at + timedelta(minutes=5)
        pending = [
            {
                "id": 11,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "5m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = self._matched_horizon_market(self.event_at)
        market.append({"timestamp": self.event_at, "close": 99.0, "source": "oanda"})
        market.append({"timestamp": target_at, "close": 100.0, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        baseline = [
            row
            for row in market
            if row["timestamp"] == self.event_at - timedelta(minutes=1)
        ][0]["close"]
        target = [row for row in market if row["timestamp"] == target_at][0]["close"]
        percentage = (target / baseline - 1.0) * 100.0
        self.assertAlmostEqual(updates["percentage_move"], percentage, places=9)
        # Timestamp-paired 5-minute returns: anchors at minutes 0..110 pair
        # with the sample exactly 5 minutes later (23 returns, alternating
        # 0.1%/0.2%).
        expected_returns = [0.1 if index % 2 == 0 else 0.2 for index in range(23)]
        expected_mean = sum(expected_returns) / len(expected_returns)
        expected_variance = sum(
            (value - expected_mean) ** 2 for value in expected_returns
        ) / len(expected_returns)
        expected_volatility = math.sqrt(expected_variance)
        self.assertAlmostEqual(
            updates["volatility_adjusted_move"],
            percentage / expected_volatility,
            places=6,
        )
        self.assertEqual(updates["volatility_version"], 2)
        provenance = json.loads(updates["provenance"])
        volatility = provenance["volatility"]
        self.assertEqual(volatility["method"], "first_at_or_after")
        self.assertEqual(volatility["horizon_seconds"], 300)
        self.assertEqual(volatility["horizon_minutes"], 5)
        self.assertEqual(volatility["lookback_minutes"], 1440)
        self.assertEqual(volatility["source_timeframe"], "PRICE")
        self.assertEqual(volatility["observation_interval_seconds"], 60)
        self.assertEqual(volatility["samples"], 120)
        self.assertEqual(volatility["returns"], 23)
        self.assertIsNotNone(volatility["lookback_start"])
        self.assertIsNotNone(volatility["lookback_end"])
        self.assertEqual(volatility["version"], 2)

    def test_irregular_cadence_timestamp_paired_volatility(self):
        # Three samples per 5-minute block at 0m/1m/2.5m offsets: cadence
        # (median gap) is 90 seconds, and the timestamp-paired 5-minute
        # returns alternate +0.1%/+0.2%.
        target_at = self.event_at + timedelta(minutes=5)
        pending = [
            {
                "id": 13,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "5m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        offsets = [0, 1, 2.5]
        block_returns = [0.001 if block % 2 == 0 else 0.002 for block in range(5)]
        price = 100.0
        market = []
        for block in range(5):
            for offset in offsets:
                ts = (
                    self.event_at
                    - timedelta(minutes=25)
                    + timedelta(minutes=block * 5 + offset)
                )
                market.append({"timestamp": ts, "close": price, "source": "oanda"})
            if block < 4:
                price = price * (1.0 + block_returns[block])
        market.append({"timestamp": self.event_at, "close": 99.0, "source": "oanda"})
        market.append({"timestamp": target_at, "close": 100.0, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        baseline = [
            row
            for row in market
            if row["timestamp"] == self.event_at - timedelta(minutes=2.5)
        ][0]["close"]
        target = [row for row in market if row["timestamp"] == target_at][0]["close"]
        percentage = (target / baseline - 1.0) * 100.0
        # Four non-overlapping pairs (block starts pair with the next block
        # start exactly 5 minutes later).
        expected_returns = [0.1 if index % 2 == 0 else 0.2 for index in range(4)]
        expected_mean = sum(expected_returns) / len(expected_returns)
        expected_variance = sum(
            (value - expected_mean) ** 2 for value in expected_returns
        ) / len(expected_returns)
        expected_volatility = math.sqrt(expected_variance)
        self.assertAlmostEqual(
            updates["volatility_adjusted_move"],
            percentage / expected_volatility,
            places=6,
        )
        provenance = json.loads(updates["provenance"])
        volatility = provenance["volatility"]
        self.assertEqual(volatility["method"], "first_at_or_after")
        self.assertEqual(volatility["observation_interval_seconds"], 90)
        self.assertEqual(volatility["samples"], 15)
        self.assertEqual(volatility["returns"], 4)

    def test_dense_lookback_downsampled_spans_full_window(self):
        # 2500 ticks at 10s cadence (~6.9h) with the default 1440-minute
        # lookback: bucket-last downsampling (44s buckets) must span the FULL
        # window, not just the latest rows.
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 15,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "5m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = []
        for index in range(2500):
            ts = (
                self.event_at - timedelta(seconds=25000) + timedelta(seconds=10 * index)
            )
            market.append(
                {"timestamp": ts, "close": 100.0 + index * 0.001, "source": "oanda"}
            )
        market.append({"timestamp": self.event_at, "close": 99.0, "source": "oanda"})
        market.append({"timestamp": target_at, "close": 100.0, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        provenance = json.loads(updates["provenance"])
        volatility = provenance["volatility"]
        bucket_seconds = 44  # ceil(1440 * 60 / 2000)
        self.assertEqual(volatility["downsample_bucket_seconds"], bucket_seconds)
        self.assertEqual(volatility["sampling_method"], "bucket_last")
        pre_rows = [row for row in market[:2500]]
        expected_samples = len(
            {int(row["timestamp"].timestamp() // bucket_seconds) for row in pre_rows}
        )
        self.assertEqual(volatility["samples"], expected_samples)
        first_bucket = int(pre_rows[0]["timestamp"].timestamp() // bucket_seconds)
        first_bucket_last = max(
            (
                row
                for row in pre_rows
                if int(row["timestamp"].timestamp() // bucket_seconds) == first_bucket
            ),
            key=lambda row: row["timestamp"],
        )
        # The window spans the FULL lookback: the first sample is the first
        # bucket's last tick (roughly the start of the data), not the latest
        # 2000 ticks.
        self.assertEqual(
            datetime.fromisoformat(volatility["lookback_start"]),
            first_bucket_last["timestamp"],
        )
        self.assertEqual(
            datetime.fromisoformat(volatility["lookback_end"]),
            self.event_at - timedelta(seconds=10),
        )

    def test_dense_tick_1h_horizon_downsampled_volatility(self):
        # 18000 one-second ticks over 5 hours (>2000 rows) with a 300-minute
        # volatility lookback: bucket-last downsampling (9s buckets, 2000
        # buckets) must let a 1h horizon form 4 clean same-horizon returns,
        # stable against an equivalent 1-minute series.
        target_at = self.event_at + timedelta(minutes=60)
        config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "volatility_lookback_minutes": 300,
                "target_tolerance_minutes": 5,
            },
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "CPI",
                    "instruments": ["EURUSD"],
                    "expected_sensitivity": {"EURUSD": "neutral"},
                }
            },
        }
        pending = [
            {
                "id": 26,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "60m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        block_returns = [0.001 if block % 2 == 0 else 0.002 for block in range(5)]
        market = []
        for second in range(18000):  # 5 hours at 1-second cadence
            block = second // 3600
            block_price = 100.0
            for index in range(block):
                block_price *= 1.0 + block_returns[index]
            ts = self.event_at - timedelta(seconds=18000) + timedelta(seconds=second)
            market.append({"timestamp": ts, "close": block_price, "source": "oanda"})
        market.append({"timestamp": self.event_at, "close": 99.0, "source": "oanda"})
        market.append({"timestamp": target_at, "close": 100.0, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        bucket_seconds = 9  # ceil(300 * 60 / 2000), divides 3600: aligned
        provenance = json.loads(updates["provenance"])
        volatility = provenance["volatility"]
        self.assertEqual(volatility["method"], "first_at_or_after")
        self.assertEqual(volatility["samples"], 2000)
        self.assertEqual(volatility["returns"], 4)
        self.assertEqual(volatility["downsample_bucket_seconds"], bucket_seconds)
        self.assertEqual(volatility["horizon_seconds"], 3600)
        # Four 1h returns alternating 0.1%/0.2% -> population std 0.05.
        expected_returns = [0.1 if index % 2 == 0 else 0.2 for index in range(4)]
        expected_mean = sum(expected_returns) / len(expected_returns)
        expected_variance = sum(
            (value - expected_mean) ** 2 for value in expected_returns
        ) / len(expected_returns)
        expected_volatility = math.sqrt(expected_variance)
        baseline = [
            row
            for row in market
            if row["timestamp"] == self.event_at - timedelta(seconds=1)
        ][0]["close"]
        target = [row for row in market if row["timestamp"] == target_at][0]["close"]
        percentage = (target / baseline - 1.0) * 100.0
        self.assertAlmostEqual(
            updates["volatility_adjusted_move"],
            percentage / expected_volatility,
            places=6,
        )
        # Stability: the equivalent 1-minute series over the same path yields
        # the identical downsampled volatility.
        minute_market = []
        for minute in range(300):
            block = minute // 60
            block_price = 100.0
            for index in range(block):
                block_price *= 1.0 + block_returns[index]
            ts = self.event_at - timedelta(minutes=300) + timedelta(minutes=minute)
            minute_market.append(
                {"timestamp": ts, "close": block_price, "source": "oanda"}
            )
        vol_minute, meta_minute = _realized_volatility(
            minute_market,
            horizon_seconds=3600,
            horizon_minutes=60,
            lookback_minutes=300,
            bucket_seconds=bucket_seconds,
            timeframe="PRICE",
        )
        self.assertIsNotNone(vol_minute)
        self.assertAlmostEqual(vol_minute, expected_volatility, places=9)

    def test_60m_horizon_uses_separate_volatility_lookback(self):
        # The volatility sample window is independent of the 120-minute
        # baseline bound: the default 1440-minute lookback feeds a 60m horizon
        # with 23 same-horizon returns, so volatility_adjusted_move is
        # computed instead of being systematically None.
        target_at = self.event_at + timedelta(minutes=60)
        pending = [
            {
                "id": 17,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "60m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = []
        returns = [0.001 if block % 2 == 0 else 0.002 for block in range(24)]
        price = 100.0
        for minute in range(1440):
            if minute > 0 and minute % 60 == 0:
                price = price * (1.0 + returns[minute // 60 - 1])
            ts = self.event_at - timedelta(minutes=1440) + timedelta(minutes=minute)
            market.append({"timestamp": ts, "close": price, "source": "oanda"})
        market.append({"timestamp": self.event_at, "close": 99.0, "source": "oanda"})
        market.append({"timestamp": target_at, "close": 100.0, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        # Baseline selection is the direct latest pre-event row (no range
        # bound, no lookback param) while the volatility sample path uses the
        # separate 1440-minute bound.
        baseline_call = next(
            (sql, params)
            for sql, params in session.calls
            if "FROM market_data" in sql and "ORDER BY timestamp DESC LIMIT 1" in sql
        )
        self.assertNotIn("lower", baseline_call[1])
        pre_call = next(
            (sql, params)
            for sql, params in session.calls
            if "FROM market_data" in sql and "bucket_seconds" in params
        )
        self.assertEqual(
            pre_call[1]["lower"],
            self.event_at - timedelta(minutes=1440),
        )
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        baseline = [
            row
            for row in market
            if row["timestamp"] == self.event_at - timedelta(minutes=1)
        ][0]["close"]
        target = [row for row in market if row["timestamp"] == target_at][0]["close"]
        percentage = (target / baseline - 1.0) * 100.0
        expected_returns = [0.1 if index % 2 == 0 else 0.2 for index in range(23)]
        expected_mean = sum(expected_returns) / len(expected_returns)
        expected_variance = sum(
            (value - expected_mean) ** 2 for value in expected_returns
        ) / len(expected_returns)
        expected_volatility = math.sqrt(expected_variance)
        self.assertAlmostEqual(
            updates["volatility_adjusted_move"],
            percentage / expected_volatility,
            places=6,
        )
        provenance = json.loads(updates["provenance"])
        volatility = provenance["volatility"]
        self.assertEqual(volatility["method"], "first_at_or_after")
        self.assertEqual(volatility["horizon_seconds"], 3600)
        self.assertEqual(volatility["horizon_minutes"], 60)
        self.assertEqual(volatility["lookback_minutes"], 1440)
        self.assertEqual(volatility["observation_interval_seconds"], 60)
        self.assertEqual(volatility["samples"], 1440)
        self.assertEqual(volatility["returns"], 23)

    def test_eos_volatility_uses_actual_horizon_seconds(self):
        # end_of_session volatility must pair on the real event_at -> target_at
        # interval (Thursday 12:00 UTC -> Friday 17:00 NY = 21:00 UTC EDT,
        # i.e. 33 hours = 118800 seconds), not a nominal 1-minute horizon.
        target_at = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
        pending = [
            {
                "id": 16,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "end_of_session",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = self._matched_horizon_market(self.event_at)
        market.append({"timestamp": self.event_at, "close": 99.0, "source": "oanda"})
        market.append({"timestamp": target_at, "close": 100.0, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertIsNone(updates["volatility_adjusted_move"])
        provenance = json.loads(updates["provenance"])
        volatility = provenance["volatility"]
        self.assertEqual(volatility["horizon_seconds"], 118800)
        self.assertIsNone(volatility["horizon_minutes"])
        self.assertEqual(volatility["lookback_minutes"], 1440)
        # 120 one-minute samples cannot pair a 33-hour horizon.
        self.assertEqual(volatility["method"], "insufficient_pairs")
        self.assertEqual(volatility["returns"], 0)
        self.assertEqual(volatility["samples"], 120)

    def test_non_finite_rows_are_skipped_by_sql_predicate(self):
        # A NaN/Infinity row closest to the event must not mask the later
        # valid baseline/target sample: the SQL finite predicate rejects it
        # before ORDER/LIMIT runs.
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 14,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=2),
                "close": 100.0,
                "source": "oanda",
            },
            # Would win the DESC baseline scan without the finite predicate.
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": float("nan"),
                "source": "oanda",
            },
            {"timestamp": self.event_at, "close": float("inf"), "source": "oanda"},
            # Would win the ASC target scan without the finite predicate.
            {"timestamp": target_at, "close": float("nan"), "source": "oanda"},
            {
                "timestamp": target_at + timedelta(seconds=10),
                "close": 99.0,
                "source": "oanda",
            },
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        # The emitted SQL itself carries the finite predicate.
        baseline_sql = next(
            sql
            for sql, _ in session.calls
            if "FROM market_data" in sql and "ORDER BY timestamp DESC" in sql
        )
        self.assertIn("'NaN'::DOUBLE PRECISION", baseline_sql)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["baseline_at"], self.event_at - timedelta(minutes=2))
        self.assertEqual(updates["baseline_price"], 100.0)
        self.assertEqual(updates["observed_at"], target_at + timedelta(seconds=10))
        self.assertEqual(updates["target_price"], 99.0)
        self.assertIsNone(updates["missing_data_reason"])

    def test_recompute_reprocesses_rows_and_recomputes_eos_target(self):
        # A stale eos target (same-day 21:00 UTC) must be re-derived to the
        # weekly FX close: Friday 17:00 America/New_York (21:00 UTC in EDT).
        event_at = self.event_at  # Thursday 2026-08-06 12:00 UTC
        stale_target = datetime(2026, 8, 6, 21, 0, tzinfo=UTC)
        expected_target = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
        pending = [
            {
                "id": 12,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "end_of_session",
                "event_at": event_at,
                "target_at": stale_target,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": datetime(2026, 8, 6, 11, 0, tzinfo=UTC),
                "close": 100.0,
                "source": "oanda",
            },
            # Final pre-close observation within the eos backward tolerance.
            {
                "timestamp": datetime(2026, 8, 7, 20, 59, tzinfo=UTC),
                "close": 99.0,
                "source": "oanda",
            },
        ]
        session = _Session(pending=pending, market=market)
        summary = recompute_reaction_windows(
            session, self.config, now=expected_target + timedelta(minutes=1), limit=10
        )
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["target_at"], expected_target)
        self.assertEqual(
            updates["observed_at"], datetime(2026, 8, 7, 20, 59, tzinfo=UTC)
        )
        # Baseline Thu 11:00 vs event Thu 12:00 -> -3600s; the observed sample
        # is 60s before the weekly close (negative target offset).
        self.assertEqual(updates["baseline_offset_seconds"], -3600)
        self.assertEqual(updates["target_offset_seconds"], -60)

    def test_stale_baseline_reason_and_provenance(self):
        # The baseline is selected as the direct latest pre-event row; the
        # lookback is then enforced as a freshness policy after selection.
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 18,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        stale_at = self.event_at - timedelta(hours=3)
        market = [
            {"timestamp": stale_at, "close": 100.0, "source": "oanda"},
            {"timestamp": self.event_at, "close": 101.0, "source": "oanda"},
            {"timestamp": target_at, "close": 99.0, "source": "oanda"},
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 0)
        self.assertEqual(summary["unresolved"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["missing_data_reason"], "stale_baseline")
        # The stale baseline is kept for audit.
        self.assertEqual(updates["baseline_at"], stale_at)
        self.assertEqual(updates["baseline_price"], 100.0)
        self.assertIsNone(updates["observed_price"])
        provenance = json.loads(updates["provenance"])
        stale = provenance["stale_baseline"]
        self.assertEqual(stale["max_age_minutes"], 120)
        self.assertEqual(stale["age_minutes"], 180)
        self.assertEqual(datetime.fromisoformat(stale["baseline_at"]), stale_at)

    def test_fresh_baseline_within_lookback_resolves(self):
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 19,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=90),
                "close": 100.0,
                "source": "oanda",
            },
            {"timestamp": target_at, "close": 99.0, "source": "oanda"},
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertIsNone(updates["missing_data_reason"])
        self.assertEqual(updates["baseline_offset_seconds"], -5400)

    def test_eos_target_selects_pre_close_tick_not_post_close(self):
        # Friday event; venue close Friday 21:00. Only a tick at close-1m and
        # one at close+1m exist: the eos target must be the close-1m tick
        # (never a post-close observation) with a negative target offset.
        event_at = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)  # Friday
        target_at = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
        pending = [
            {
                "id": 23,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "end_of_session",
                "event_at": event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            },
            {
                "timestamp": target_at - timedelta(minutes=1),
                "close": 99.0,
                "source": "oanda",
            },
            {
                "timestamp": target_at + timedelta(minutes=1),
                "close": 98.0,
                "source": "oanda",
            },
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=2)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["observed_at"], target_at - timedelta(minutes=1))
        self.assertEqual(updates["target_price"], 99.0)
        self.assertEqual(updates["target_offset_seconds"], -60)
        self.assertIsNone(updates["missing_data_reason"])

    def test_friday_close_and_weekend_never_cross(self):
        # A Friday target must never accept Monday data: the eos close-backward
        # window and the intraday wall-clock tolerance both stay on Friday.
        event_at = datetime(2026, 8, 7, 20, 58, tzinfo=UTC)  # Friday
        # A Monday tick that would sit inside the OLD calendar.forward window
        # (which jumped across the weekend): Monday 00:02 is within
        # forward(Fri 20:59, 5) but outside the plain Friday bound.
        monday = datetime(2026, 8, 10, 0, 2, tzinfo=UTC)
        for horizon, target_at in (
            ("end_of_session", datetime(2026, 8, 7, 21, 0, tzinfo=UTC)),
            ("1m", event_at + timedelta(minutes=1)),
        ):
            pending = [
                {
                    "id": 24,
                    "event_id": self.event_id,
                    "instrument_symbol": "EURUSD",
                    "timeframe": "PRICE",
                    "horizon": horizon,
                    "event_at": event_at,
                    "target_at": target_at,
                    "baseline_at": None,
                    "baseline_price": None,
                    "target_price": None,
                    "observed_at": None,
                    "observed_price": None,
                    "expected_direction": "neutral",
                    "sensitivity": "neutral",
                    "reaction_state": "pending",
                    "missing_data_reason": None,
                    "provenance": {},
                }
            ]
            market = [
                # Baseline sits outside the eos backward tolerance band
                # ([20:55, 21:00]) so only the Monday tick could satisfy it.
                {
                    "timestamp": event_at - timedelta(minutes=8),
                    "close": 100.0,
                    "source": "oanda",
                },
                {"timestamp": monday, "close": 99.0, "source": "oanda"},
            ]
            session = _Session(pending=pending, market=market)
            summary = backfill_reaction_windows(
                session, self.config, now=monday + timedelta(minutes=1)
            )
            self.assertEqual(summary["completed"], 0, horizon)
            self.assertEqual(summary["unresolved"], 1, horizon)
            updates = [
                params
                for sql, params in session.calls
                if "UPDATE event_reaction_windows" in sql
            ][0]
            self.assertEqual(updates["missing_data_reason"], "missing_target", horizon)
            self.assertIsNone(updates["observed_price"], horizon)

    def test_subsecond_baseline_offset_floors_negative(self):
        # A strictly pre-event baseline 0.4s before the event must persist as
        # -1 second (never round to 0 and violate the sign check).
        target_at = self.event_at + timedelta(minutes=1)
        pending = [
            {
                "id": 25,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(milliseconds=400),
                "close": 100.0,
                "source": "oanda",
            },
            {"timestamp": target_at, "close": 99.0, "source": "oanda"},
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["baseline_offset_seconds"], -1)
        self.assertEqual(
            updates["baseline_at"], self.event_at - timedelta(milliseconds=400)
        )
        self.assertIsNone(updates["missing_data_reason"])

    def test_irregular_cadence_prefers_first_at_or_after_target(self):
        # A closer pre-target sample must NOT be paired: the end sample is
        # the FIRST at or after the target (full-horizon returns only).
        t0 = self.event_at - timedelta(minutes=30)
        rows = [
            {"timestamp": t0, "close": 100.0, "source": "oanda"},
            {
                "timestamp": t0 + timedelta(seconds=299),
                "close": 101.0,
                "source": "oanda",
            },
            {
                "timestamp": t0 + timedelta(seconds=305),
                "close": 102.0,
                "source": "oanda",
            },
            {
                "timestamp": t0 + timedelta(seconds=604),
                "close": 103.0,
                "source": "oanda",
            },
            {
                "timestamp": t0 + timedelta(seconds=610),
                "close": 105.0,
                "source": "oanda",
            },
        ]
        vol, meta = _realized_volatility(
            rows,
            horizon_seconds=300,
            horizon_minutes=5,
            lookback_minutes=30,
            bucket_seconds=1,
            timeframe="PRICE",
        )
        self.assertEqual(meta["method"], "first_at_or_after")
        self.assertEqual(meta["returns"], 2)
        # Pair 1 uses t0+305 (2.0%), not the closer t0+299 (1.0%); pair 2
        # uses t0+610 (105/102-1).
        expected = [2.0, (105.0 / 102.0 - 1.0) * 100.0]
        mean = sum(expected) / 2
        variance = sum((value - mean) ** 2 for value in expected) / 2
        self.assertAlmostEqual(vol, math.sqrt(variance), places=9)

    def test_weekend_friday_close_baseline_fresh_by_trading_time(self):
        # A Monday-open event's Friday-close baseline is fresh when the
        # TRADING-time distance is within the lookback (wall-clock gap ~3
        # days carries no trading time); same for a holiday gap.
        config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "target_tolerance_minutes": 5,
            },
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "CPI",
                    "instruments": ["SP500"],
                    "expected_sensitivity": {"SP500": "neutral"},
                }
            },
        }
        scenarios = (
            # Weekend: Friday 2026-08-07 16:00 ET close -> Monday 09:35 ET.
            (
                datetime(2026, 8, 10, 13, 35, tzinfo=UTC),  # Monday 09:35 ET
                datetime(2026, 8, 7, 20, 0, tzinfo=UTC),  # Friday 16:00 ET
            ),
            # Holiday: Thursday 2026-07-02 16:00 ET -> Monday 07-06 09:35 ET
            # (July 3 is the observed Independence Day).
            (
                datetime(2026, 7, 6, 13, 35, tzinfo=UTC),
                datetime(2026, 7, 2, 20, 0, tzinfo=UTC),
            ),
        )
        for event_at, baseline_at in scenarios:
            target_at = event_at + timedelta(minutes=1)
            pending = [
                {
                    "id": 28,
                    "event_id": self.event_id,
                    "instrument_symbol": "SP500",
                    "timeframe": "PRICE",
                    "horizon": "1m",
                    "event_at": event_at,
                    "target_at": target_at,
                    "baseline_at": None,
                    "baseline_price": None,
                    "target_price": None,
                    "observed_at": None,
                    "observed_price": None,
                    "expected_direction": "neutral",
                    "sensitivity": "neutral",
                    "reaction_state": "pending",
                    "missing_data_reason": None,
                    "provenance": {},
                }
            ]
            market = [
                {"timestamp": baseline_at, "close": 100.0, "source": "oanda"},
                {"timestamp": target_at, "close": 99.0, "source": "oanda"},
            ]
            session = _Session(pending=pending, market=market)
            summary = backfill_reaction_windows(
                session, config, now=target_at + timedelta(minutes=1)
            )
            self.assertEqual(summary["completed"], 1, event_at)
            updates = [
                params
                for sql, params in session.calls
                if "UPDATE event_reaction_windows" in sql
            ][0]
            self.assertIsNone(updates["missing_data_reason"], event_at)
            self.assertEqual(updates["baseline_at"], baseline_at, event_at)
            # The wall-clock offset is persisted separately (negative).
            self.assertLess(updates["baseline_offset_seconds"], -100000, event_at)

    def test_late_reversal_beyond_path_row_cap(self):
        # More than 500 post-event path rows: the final sign (a late reversal)
        # must be honored by the SQL aggregate instead of being capped to the
        # earliest rows.
        target_at = self.event_at + timedelta(minutes=10)
        pending = [
            {
                "id": 29,
                "event_id": self.event_id,
                "instrument_symbol": "EURUSD",
                "timeframe": "PRICE",
                "horizon": "5m",
                "event_at": self.event_at,
                "target_at": target_at,
                "baseline_at": None,
                "baseline_price": None,
                "target_price": None,
                "observed_at": None,
                "observed_price": None,
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            }
        ]
        # 900 path ticks: first half above baseline, second half below.
        for index in range(900):
            ts = self.event_at + timedelta(seconds=index + 1)
            close = 101.0 if index < 450 else 99.0
            market.append({"timestamp": ts, "close": close, "source": "oanda"})
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, self.config, now=target_at + timedelta(minutes=10)
        )
        self.assertEqual(summary["completed"], 1)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ][0]
        self.assertEqual(updates["reaction_state"], "reversal")

    def test_instrument_price_timeframe_is_executable(self):
        # A configured per-instrument price_timeframe drives the market_data
        # sample queries while the window identity timeframe stays untouched.
        target_at = self.event_at + timedelta(minutes=1)
        config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "target_tolerance_minutes": 5,
                "calendars": {
                    "instruments": {
                        "EURUSD": {"venue": "fx_24x5", "price_timeframe": "1m"}
                    }
                },
            },
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "CPI",
                    "instruments": ["EURUSD"],
                    "expected_sensitivity": {"EURUSD": "negative"},
                }
            },
        }
        pending = [
            {
                "id": 20,
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
                "expected_direction": "neutral",
                "sensitivity": "neutral",
                "reaction_state": "pending",
                "missing_data_reason": None,
                "provenance": {},
            }
        ]
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            },
            {"timestamp": target_at, "close": 99.0, "source": "oanda"},
        ]
        session = _Session(pending=pending, market=market)
        summary = backfill_reaction_windows(
            session, config, now=target_at + timedelta(minutes=1)
        )
        self.assertEqual(summary["completed"], 1)
        market_calls = [
            params
            for sql, params in session.calls
            if "FROM market_data" in sql and "SELECT" in sql
        ]
        self.assertTrue(market_calls)
        self.assertTrue(all(params["timeframe"] == "1m" for params in market_calls))
        provenance = json.loads(
            [
                params
                for sql, params in session.calls
                if "UPDATE event_reaction_windows" in sql
            ][0]["provenance"]
        )
        self.assertEqual(provenance["calendar"]["price_timeframe"], "1m")

    def test_recompute_legacy_only_and_dry_run(self):
        target_at = self.event_at + timedelta(minutes=1)
        legacy_row = {
            "id": 21,
            "event_id": self.event_id,
            "instrument_symbol": "EURUSD",
            "timeframe": "PRICE",
            "horizon": "1m",
            "event_at": self.event_at,
            "target_at": target_at,
            "baseline_at": self.event_at - timedelta(minutes=1),
            "baseline_price": 100.0,
            "target_price": 99.0,
            "observed_at": target_at,
            "observed_price": 99.0,
            "expected_direction": "neutral",
            "sensitivity": "neutral",
            "reaction_state": "persistence",
            "missing_data_reason": None,
            "provenance": {},
        }
        current_row = dict(legacy_row)
        current_row["id"] = 22
        current_row["volatility_version"] = 2  # already v2: not legacy
        market = [
            {
                "timestamp": self.event_at - timedelta(minutes=1),
                "close": 100.0,
                "source": "oanda",
            },
            {"timestamp": target_at, "close": 99.0, "source": "oanda"},
        ]
        session = _Session(pending=[legacy_row, current_row], market=market)
        summary = recompute_reaction_windows(
            session,
            self.config,
            now=target_at + timedelta(minutes=1),
            limit=10,
            legacy_only=True,
        )
        self.assertEqual(summary["scanned"], 1)  # only the NULL-version row
        self.assertEqual(summary["legacy_only"], True)
        updates = [
            params
            for sql, params in session.calls
            if "UPDATE event_reaction_windows" in sql
        ]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["volatility_version"], 2)
        # Dry-run computes and reports without mutating.
        session2 = _Session(pending=[legacy_row], market=market)
        summary = recompute_reaction_windows(
            session2,
            self.config,
            now=target_at + timedelta(minutes=1),
            limit=10,
            legacy_only=True,
            dry_run=True,
        )
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["dry_run"], True)
        self.assertEqual(
            [
                sql
                for sql, _ in session2.calls
                if "UPDATE event_reaction_windows" in sql
            ],
            [],
        )


class VenueCalendarTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "reaction_windows": {},
        }

    def test_fx_weekly_market_week_rules(self):
        calendar = venue_for_symbol("EURUSD", self.config)
        self.assertEqual(calendar.name, "fx_24x5")
        self.assertTrue(calendar.rule.weekly)
        # Sunday 17:00 America/New_York open (21:00 UTC in EDT summer).
        self.assertFalse(
            calendar.is_open(datetime(2026, 8, 9, 20, 0, tzinfo=UTC))
        )  # 16:00 EDT
        self.assertTrue(
            calendar.is_open(datetime(2026, 8, 9, 23, 0, tzinfo=UTC))
        )  # 19:00 EDT
        # Continuous weekdays: no daily 3-hour close (22:00 UTC is open).
        self.assertTrue(
            calendar.is_open(datetime(2026, 8, 10, 22, 0, tzinfo=UTC))
        )  # Mon 18:00 EDT
        # Friday 17:00 America/New_York close (21:00 UTC in EDT summer).
        self.assertTrue(
            calendar.is_open(datetime(2026, 8, 14, 16, 0, tzinfo=UTC))
        )  # Fri 12:00 EDT
        self.assertFalse(
            calendar.is_open(datetime(2026, 8, 14, 23, 0, tzinfo=UTC))
        )  # Fri 19:00 EDT
        # Weekend closed.
        self.assertFalse(
            calendar.is_open(datetime(2026, 8, 15, 12, 0, tzinfo=UTC))
        )  # Saturday
        # Rewinding from an exact midnight slice boundary consumes the
        # contiguous prior slice instead of looping on the same timestamp.
        self.assertEqual(
            calendar.backward(datetime(2026, 8, 10, 4, 0, tzinfo=UTC), 60),
            datetime(2026, 8, 10, 3, 0, tzinfo=UTC),
        )
        # EOS resolves the weekly close: Friday 17:00 America/New_York.
        self.assertEqual(
            horizon_target(
                datetime(2026, 8, 14, 20, 0, tzinfo=UTC), "end_of_session", self.config
            ),
            datetime(2026, 8, 14, 21, 0, tzinfo=UTC),  # 17:00 EDT
        )
        self.assertEqual(
            horizon_target(
                datetime(2026, 8, 15, 12, 0, tzinfo=UTC), "end_of_session", self.config
            ),
            datetime(2026, 8, 21, 21, 0, tzinfo=UTC),  # next Friday
        )
        self.assertEqual(
            horizon_target(
                datetime(2026, 8, 9, 12, 0, tzinfo=UTC), "end_of_session", self.config
            ),
            datetime(2026, 8, 14, 21, 0, tzinfo=UTC),  # the week that opened Sunday
        )
        # DST: 17:00 EST is 22:00 UTC in winter.
        self.assertEqual(
            horizon_target(
                datetime(2026, 12, 18, 20, 0, tzinfo=UTC), "end_of_session", self.config
            ),
            datetime(2026, 12, 18, 22, 0, tzinfo=UTC),  # 17:00 EST
        )

    def test_fx_trading_time_walks_skip_weekend(self):
        calendar = venue_for_symbol("EURUSD", self.config)
        # Monday 00:30 UTC (Sunday 20:30 EDT, inside Sunday's session) minus
        # 120 trading minutes lands Sunday 18:30 EDT.
        self.assertEqual(
            calendar.backward(datetime(2026, 8, 10, 0, 30, tzinfo=UTC), 120),
            datetime(2026, 8, 9, 22, 30, tzinfo=UTC),
        )
        # A Saturday offset extends to the Sunday open plus tolerance.
        self.assertEqual(
            calendar.forward(datetime(2026, 8, 15, 12, 1, tzinfo=UTC), 5),
            datetime(2026, 8, 16, 21, 5, tzinfo=UTC),  # Sun 17:05 EDT
        )
        # DST: the Sunday open is 22:00 UTC in winter (EST).
        self.assertEqual(
            calendar.next_open(datetime(2026, 11, 7, 12, 0, tzinfo=UTC)),
            datetime(2026, 11, 8, 22, 0, tzinfo=UTC),
        )

    def test_nyse_holidays_early_close_and_dst(self):
        calendar = venue_for_symbol("SP500", self.config)
        self.assertEqual(calendar.name, "nyse")
        for day in (
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 2, 16),
            date(2026, 4, 3),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 3),  # observed Independence Day (Jul 4 is Saturday)
            date(2026, 9, 7),
            date(2026, 11, 26),
            date(2026, 12, 25),
        ):
            self.assertFalse(calendar.is_trading_day(day), day)
        self.assertTrue(calendar.is_trading_day(date(2026, 7, 6)))
        # Early close: day after Thanksgiving at 13:00 ET.
        self.assertEqual(
            calendar.session_close_after(datetime(2026, 11, 26, 13, 0, tzinfo=UTC)),
            datetime(2026, 11, 27, 18, 0, tzinfo=UTC),  # 13:00 EST
        )
        # DST: 16:00 ET is 20:00 UTC in EDT (summer) and 21:00 UTC in EST.
        self.assertEqual(
            calendar.session_close_after(datetime(2026, 6, 15, 12, 0, tzinfo=UTC)),
            datetime(2026, 6, 15, 20, 0, tzinfo=UTC),
        )
        self.assertEqual(
            calendar.session_close_after(datetime(2026, 12, 15, 12, 0, tzinfo=UTC)),
            datetime(2026, 12, 15, 21, 0, tzinfo=UTC),
        )
        # Holiday close rolls to the next session close.
        self.assertEqual(
            calendar.session_close_after(datetime(2026, 7, 3, 12, 0, tzinfo=UTC)),
            datetime(2026, 7, 6, 20, 0, tzinfo=UTC),
        )
        # 2025 reference dates.
        self.assertFalse(calendar.is_trading_day(date(2025, 4, 18)))  # Good Friday
        self.assertFalse(calendar.is_trading_day(date(2025, 11, 27)))  # Thanksgiving
        self.assertEqual(calendar.rule.early_closes[date(2025, 11, 28)], time(13, 0))

    def test_lse_and_xetra_holidays(self):
        lse = venue_for_symbol("UK100", self.config)
        self.assertEqual(lse.name, "lse")
        self.assertFalse(lse.is_trading_day(date(2026, 12, 25)))
        self.assertFalse(lse.is_trading_day(date(2026, 12, 28)))  # Boxing substitute
        xetra = venue_for_symbol("GER40", self.config)
        self.assertEqual(xetra.name, "xetra")
        self.assertTrue(xetra.is_trading_day(date(2026, 5, 14)))  # Trades on Ascension
        self.assertFalse(xetra.is_trading_day(date(2026, 12, 24)))  # Christmas Eve

    def test_one_off_nyse_closures(self):
        calendar = venue_for_symbol("SP500", self.config)
        for day in (
            date(1994, 4, 27),  # Nixon funeral
            date(2001, 9, 11),  # September 11 attacks
            date(2001, 9, 12),
            date(2001, 9, 13),
            date(2001, 9, 14),
            date(2004, 6, 11),  # Reagan National Day of Mourning
            date(2007, 1, 2),  # Ford National Day of Mourning
            date(2012, 10, 29),  # Hurricane Sandy
            date(2012, 10, 30),
            date(2018, 12, 5),  # George H. W. Bush National Day of Mourning
            date(2025, 1, 9),  # Carter National Day of Mourning
        ):
            self.assertFalse(calendar.is_trading_day(day), day)
        # Reopen days around the closures remain sessions.
        self.assertTrue(calendar.is_trading_day(date(2001, 9, 17)))
        self.assertTrue(calendar.is_trading_day(date(2012, 10, 31)))

    def test_one_off_lse_closures(self):
        calendar = venue_for_symbol("UK100", self.config)
        for day in (
            date(2011, 4, 29),  # Royal wedding
            date(2022, 9, 19),  # Queen Elizabeth II funeral
            date(2023, 5, 8),  # Coronation bank holiday
        ):
            self.assertFalse(calendar.is_trading_day(day), day)
        self.assertTrue(calendar.is_trading_day(date(2022, 9, 20)))

    def test_nyse_holiday_era_rules(self):
        calendar = venue_for_symbol("SP500", self.config)
        # MLK Day is observed by the NYSE only since 1998.
        self.assertTrue(calendar.is_trading_day(date(1994, 1, 17)))  # MLK 1994
        self.assertTrue(calendar.is_trading_day(date(1997, 1, 20)))  # MLK 1997
        self.assertFalse(calendar.is_trading_day(date(1999, 1, 18)))  # MLK 1999
        # Juneteenth is a NYSE holiday only since 2022 (SR-NYSE-2021-56); the
        # NYSE traded on 2021-06-18, and pre-2021 years traded too.
        self.assertTrue(calendar.is_trading_day(date(2019, 6, 19)))
        self.assertTrue(calendar.is_trading_day(date(2021, 6, 18)))
        self.assertFalse(calendar.is_trading_day(date(2022, 6, 20)))  # observed

    def test_lse_jubilee_and_ve_day_rules(self):
        calendar = venue_for_symbol("UK100", self.config)
        # 2002 Golden Jubilee: May Day moved to Jun 3, extra holiday Jun 4.
        self.assertFalse(calendar.is_trading_day(date(2002, 6, 3)))
        self.assertFalse(calendar.is_trading_day(date(2002, 6, 4)))
        self.assertTrue(
            calendar.is_trading_day(date(2002, 5, 6))
        )  # original first Monday
        # 2012 Diamond Jubilee: moved to Jun 4/5.
        self.assertFalse(calendar.is_trading_day(date(2012, 6, 4)))
        self.assertFalse(calendar.is_trading_day(date(2012, 6, 5)))
        self.assertTrue(calendar.is_trading_day(date(2012, 5, 7)))
        # 2020 VE Day 75: May Day moved to May 8.
        self.assertFalse(calendar.is_trading_day(date(2020, 5, 8)))
        self.assertTrue(calendar.is_trading_day(date(2020, 5, 4)))
        # 2022 Platinum Jubilee: moved to Jun 2/3.
        self.assertFalse(calendar.is_trading_day(date(2022, 6, 2)))
        self.assertFalse(calendar.is_trading_day(date(2022, 6, 3)))
        self.assertTrue(calendar.is_trading_day(date(2022, 5, 2)))
        # Regular years keep the first/last Monday rules.
        self.assertFalse(calendar.is_trading_day(date(2011, 5, 2)))
        self.assertFalse(calendar.is_trading_day(date(2019, 5, 27)))

    def test_early_close_rules(self):
        nyse = venue_for_symbol("SP500", self.config)
        # July 2 stays a regular session even when July 4 falls on a weekend
        # (2020-07-02 and 2021-07-02 were full 16:00 sessions).
        self.assertNotIn(date(2020, 7, 2), nyse.rule.early_closes)
        self.assertNotIn(date(2021, 7, 2), nyse.rule.early_closes)
        self.assertEqual(
            nyse.session_close_after(datetime(2020, 7, 2, 12, 0, tzinfo=UTC)),
            datetime(2020, 7, 2, 20, 0, tzinfo=UTC),  # regular 16:00 EDT
        )
        self.assertEqual(
            nyse.session_close_after(datetime(2021, 7, 2, 12, 0, tzinfo=UTC)),
            datetime(2021, 7, 2, 20, 0, tzinfo=UTC),
        )
        # Pre-Independence: July 3 early close when July 3 is an open session.
        self.assertEqual(nyse.rule.early_closes[date(2025, 7, 3)], time(13, 0))
        self.assertEqual(
            nyse.session_close_after(datetime(2025, 7, 3, 12, 0, tzinfo=UTC)),
            datetime(2025, 7, 3, 17, 0, tzinfo=UTC),  # 13:00 EDT
        )
        self.assertEqual(nyse.rule.early_closes[date(2028, 7, 3)], time(13, 0))
        # When July 4 is Saturday (2020, 2026), July 3 is the observed full
        # holiday: no early close on it.
        self.assertNotIn(date(2026, 7, 3), nyse.rule.early_closes)
        # Christmas Eve early close is claimed through the 2026 audit boundary.
        self.assertEqual(nyse.rule.early_closes[date(2024, 12, 24)], time(13, 0))
        self.assertEqual(
            nyse.session_close_after(datetime(2024, 12, 24, 12, 0, tzinfo=UTC)),
            datetime(2024, 12, 24, 18, 0, tzinfo=UTC),  # 13:00 EST
        )
        self.assertEqual(nyse.rule.early_closes[date(2025, 12, 24)], time(13, 0))
        self.assertEqual(
            nyse.session_close_after(datetime(2025, 12, 24, 12, 0, tzinfo=UTC)),
            datetime(2025, 12, 24, 18, 0, tzinfo=UTC),
        )
        self.assertEqual(nyse.rule.early_closes[date(2026, 12, 24)], time(13, 0))
        # LSE half-days: Christmas Eve and New Year's Eve close at 12:30.
        lse = venue_for_symbol("UK100", self.config)
        self.assertEqual(lse.rule.early_closes[date(2024, 12, 24)], time(12, 30))
        self.assertEqual(lse.rule.early_closes[date(2024, 12, 31)], time(12, 30))
        self.assertEqual(
            lse.session_close_after(datetime(2024, 12, 24, 9, 0, tzinfo=UTC)),
            datetime(2024, 12, 24, 12, 30, tzinfo=UTC),
        )
        # Black Friday early close remains for all supported years.
        self.assertEqual(nyse.rule.early_closes[date(2026, 11, 27)], time(13, 0))

    def test_instrument_policy_typed_metadata_full_field_flow(self):
        from venue_calendar import instrument_policy_for

        config = {
            "reaction_windows": {
                "calendars": {
                    "default_venue": "fx_24x5",
                    "instruments": {
                        "EURUSD": {
                            "venue": "fx_24x5",
                            "price_timeframe": "1m",
                            "target_selection_policy": "first",
                        },
                        # exchange_calendar selects the rule (nyse) even
                        # though venue names the trading entity; per-instrument
                        # timezone/session overrides are applied on top.
                        "SP500": {
                            "venue": "nyse",
                            "exchange_calendar": "nyse",
                            "timezone": "America/New_York",
                            "session_open": "10:00:00",
                            "session_close": "15:30:00",
                        },
                        "UK100": "lse",  # bare-name shorthand
                    },
                },
            }
        }
        policy = instrument_policy_for("EURUSD", config, default_timeframe="PRICE")
        self.assertEqual(policy.venue, "fx_24x5")
        # FX is a weekly session: Sunday 17:00 NY open, Friday 17:00 NY close.
        self.assertEqual(policy.timezone, "America/New_York")
        self.assertEqual(policy.session_open, time(17, 0))
        self.assertEqual(policy.session_close, time(17, 0))
        self.assertTrue(policy.weekly)
        self.assertEqual(policy.week_open_day, "Sunday")
        self.assertEqual(policy.week_close_day, "Friday")
        self.assertEqual(policy.price_timeframe, "1m")
        self.assertEqual(policy.target_selection_policy, "first")
        self.assertEqual(policy.calendar_version, "1")
        self.assertEqual(policy.closure_audit_end_year, 2026)
        metadata = policy.to_metadata()
        self.assertEqual(metadata["target_selection_policy"], "first")
        self.assertEqual(metadata["session_model"], "weekly")
        self.assertEqual(metadata["session_week"]["open_day"], "Sunday")
        # exchange_calendar + per-instrument overrides are executed: the rule
        # is NYSE's (holidays included) with the overridden session bounds.
        index_policy = instrument_policy_for("SP500", config, default_timeframe="PRICE")
        self.assertEqual(index_policy.venue, "nyse")
        self.assertEqual(index_policy.session_open, time(10, 0))
        self.assertEqual(index_policy.session_close, time(15, 30))
        self.assertFalse(index_policy.to_metadata()["session_open"] == "09:30:00")
        # Bare-name shorthand: venue with defaulted policy fields.
        lse_policy = instrument_policy_for("UK100", config, default_timeframe="PRICE")
        self.assertEqual(lse_policy.venue, "lse")
        self.assertEqual(lse_policy.price_timeframe, "PRICE")
        self.assertEqual(lse_policy.target_selection_policy, "first")

    def test_instrument_policy_rejects_unknown_policy(self):
        from venue_calendar import instrument_policy_for

        config = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {
                        "EURUSD": {
                            "venue": "fx_24x5",
                            "target_selection_policy": "bogus",
                        }
                    }
                },
            }
        }
        with self.assertRaises(ValueError):
            instrument_policy_for("EURUSD", config)

    def test_unknown_venue_fails_closed(self):
        from venue_calendar import instrument_policy_for

        # A venue typo must not silently fall back to the FX rule.
        with self.assertRaises(ValueError):
            venue_for_symbol(
                "EURUSD",
                {
                    "reaction_windows": {
                        "calendars": {"instruments": {"EURUSD": "nysee"}}
                    }
                },
            )
        with self.assertRaises(ValueError):
            venue_for_symbol(
                "EURUSD",
                {
                    "reaction_windows": {
                        "calendars": {"instruments": {"EURUSD": {"venue": "nysee"}}}
                    }
                },
            )
        with self.assertRaises(ValueError):
            venue_for_symbol(
                "EURUSD",
                {
                    "reaction_windows": {
                        "calendars": {"default_venue": "bogus", "instruments": {}}
                    }
                },
            )
        with self.assertRaises(ValueError):
            instrument_policy_for(
                "EURUSD",
                {
                    "reaction_windows": {
                        "calendars": {
                            "instruments": {
                                "EURUSD": {
                                    "venue": "fx_24x5",
                                    "exchange_calendar": "lse_",
                                }
                            }
                        }
                    }
                },
            )

    def test_custom_venue_name_identity(self):
        from venue_calendar import instrument_policy_for

        config = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {
                        "EURUSD": {"venue": "asia24x5"},
                        "SP500": {"venue": "nyse", "exchange_calendar": "asia24x5"},
                    },
                    "venues": {
                        "asia24x5": {
                            "timezone": "UTC",
                            "open_time": "00:00:00",
                            "close_time": "22:00:00",
                        }
                    },
                }
            }
        }
        calendar = venue_for_symbol("EURUSD", config)
        # The custom key keeps its identity in provenance (not the FX base).
        self.assertEqual(calendar.name, "asia24x5")
        self.assertEqual(calendar.rule.close_time, time(22, 0))
        policy = instrument_policy_for("EURUSD", config)
        self.assertEqual(policy.venue, "asia24x5")
        self.assertEqual(policy.timezone, "UTC")
        self.assertEqual(policy.session_close, time(22, 0))
        self.assertEqual(policy.to_metadata()["venue"], "asia24x5")
        # exchange_calendar selects the custom rule even when venue is built-in.
        sp = venue_for_symbol("SP500", config)
        self.assertEqual(sp.name, "asia24x5")
        self.assertEqual(sp.rule.close_time, time(22, 0))

    def test_config_weekday_overrides_keep_monday_open_saturday_closed(self):
        # ISO weekdays [1..5] must keep Monday open and Saturday closed
        # (previously the raw ISO values leaked into Python weekday()).
        config = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {"EURUSD": "test_week"},
                    "venues": {
                        "test_week": {
                            "timezone": "UTC",
                            "open_time": "00:00:00",
                            "close_time": "21:00:00",
                            "weekdays": [1, 2, 3, 4, 5],
                        }
                    },
                }
            }
        }
        calendar = venue_for_symbol("EURUSD", config)
        self.assertEqual(calendar.rule.weekdays, (0, 1, 2, 3, 4))
        self.assertTrue(calendar.is_trading_day(date(2026, 8, 10)))  # Monday
        self.assertFalse(calendar.is_trading_day(date(2026, 8, 15)))  # Saturday
        self.assertFalse(calendar.is_trading_day(date(2026, 8, 16)))  # Sunday
        self.assertTrue(calendar.is_open(datetime(2026, 8, 10, 12, 0, tzinfo=UTC)))
        self.assertFalse(calendar.is_open(datetime(2026, 8, 15, 12, 0, tzinfo=UTC)))

    def test_config_weekday_override_custom_days(self):
        config = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {"EURUSD": "test_week"},
                    "venues": {
                        "test_week": {
                            "timezone": "UTC",
                            "open_time": "00:00:00",
                            "close_time": "21:00:00",
                            "weekdays": [1, 5],  # Monday + Friday
                        }
                    },
                }
            }
        }
        calendar = venue_for_symbol("EURUSD", config)
        self.assertEqual(calendar.rule.weekdays, (0, 4))
        self.assertTrue(calendar.is_trading_day(date(2026, 8, 10)))  # Monday
        self.assertTrue(calendar.is_trading_day(date(2026, 8, 14)))  # Friday
        self.assertFalse(calendar.is_trading_day(date(2026, 8, 11)))  # Tuesday
        self.assertFalse(calendar.is_trading_day(date(2026, 8, 15)))  # Saturday
        # Out-of-range weekdays (e.g. Saturday=6 is valid ISO but 0 here) fall
        # back to the default set for out-of-range input like 8.
        lax = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {"EURUSD": "test_week"},
                    "venues": {
                        "test_week": {
                            "timezone": "UTC",
                            "open_time": "00:00:00",
                            "close_time": "21:00:00",
                            "weekdays": [8],
                        }
                    },
                }
            }
        }
        self.assertEqual(venue_for_symbol("EURUSD", lax).rule.weekdays, (0, 1, 2, 3, 4))

    def test_calendar_walk_fail_closed_on_exhaustion(self):
        from venue_calendar import CalendarBoundError

        # A Monday-only venue whose every Monday within the walk window is a
        # holiday: every walk must raise (fail-closed), never fabricate a
        # timestamp off the calendar.
        holidays = []
        day = date(2025, 8, 4)  # Mondays before and after the probe
        while day < date(2027, 8, 20):
            holidays.append(day.isoformat())
            day += timedelta(days=7)
        config = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {"EURUSD": "closed_week"},
                    "venues": {
                        "closed_week": {
                            "timezone": "UTC",
                            "open_time": "00:00:00",
                            "close_time": "12:00:00",
                            "weekdays": [1],
                            "holidays": holidays,
                        }
                    },
                }
            }
        }
        calendar = venue_for_symbol("EURUSD", config)
        at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        with self.assertRaises(CalendarBoundError):
            calendar.next_open(at)
        with self.assertRaises(CalendarBoundError):
            calendar.previous_close(at)
        with self.assertRaises(CalendarBoundError):
            calendar.session_close_after(at)
        with self.assertRaises(CalendarBoundError):
            calendar.session_open_after(at)
        with self.assertRaises(CalendarBoundError):
            calendar.forward(at, 30)
        with self.assertRaises(CalendarBoundError):
            calendar.backward(at, 30)

    def test_config_calendar_overrides(self):
        config = {
            "reaction_windows": {
                "calendars": {
                    "default_venue": "fx_24x5",
                    "instruments": {"SP500": "nyse"},
                    "venues": {
                        "nyse": {
                            "timezone": "America/New_York",
                            "open_time": "09:30:00",
                            "close_time": "15:00:00",
                            "holidays": ["2026-01-02"],
                            "early_closes": {"2026-12-24": "12:00:00"},
                        }
                    },
                },
            }
        }
        calendar = venue_for_symbol("SP500", config)
        self.assertEqual(calendar.rule.close_time, time(15, 0))
        self.assertFalse(calendar.is_trading_day(date(2026, 1, 2)))
        self.assertEqual(calendar.rule.early_closes[date(2026, 12, 24)], time(12, 0))
        # Every venue's close_time is the executable session-close source;
        # the FX weekly close defaults to Friday 17:00 NY (built-in), and a
        # venues.fx_24x5 close_time override drives it.
        fx = venue_for_symbol("EURUSD", config)
        self.assertEqual(fx.rule.close_time, time(17, 0))
        fx_config = {
            "reaction_windows": {
                "calendars": {
                    "instruments": {"EURUSD": "fx_24x5"},
                    "venues": {"fx_24x5": {"close_time": "22:00:00"}},
                }
            }
        }
        self.assertEqual(
            venue_for_symbol("EURUSD", fx_config).rule.close_time, time(22, 0)
        )

    def test_calendar_scope_and_fail_fast(self):
        calendar = venue_for_symbol("SP500", self.config)
        metadata = calendar.session_metadata()
        self.assertTrue(metadata["holiday_scope"].startswith("builtin_rules:1990-"))
        self.assertEqual(
            metadata["closure_policy"]["one_off_closures_audited_through"], 2026
        )
        # Years outside the supported deterministic range fail fast instead of
        # silently treating holidays as sessions.
        with self.assertRaises(ValueError):
            calendar.is_trading_day(date(2102, 1, 2))
        with self.assertRaises(ValueError):
            calendar.session_close_after(datetime(2102, 1, 2, 12, 0, tzinfo=UTC))
        # Unknown timezones fail fast instead of silently falling back to UTC.
        with self.assertRaises(ValueError):
            venue_for_symbol(
                "SP500",
                {
                    "reaction_windows": {
                        "calendars": {
                            "instruments": {"SP500": "nyse"},
                            "venues": {"nyse": {"timezone": "Not/AZone"}},
                        }
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
