"""Real-PostgreSQL integration tests for reaction-window SQL (env-gated).

Gated on ``TEST_DATABASE_URL`` (skipped locally when unset; CI runs it against
the shared timescale service). The schema is provisioned once per module via
``pg_support.provision`` (db/init + full migrations, including 031/044) and
the reaction tables are truncated between tests. These tests exercise the real
directional SQL and constraints — no mocked SQL assertions.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pg_support
from reaction_windows import (
    backfill_reaction_windows,
    initialize_reaction_windows,
    list_event_reactions,
    recompute_reaction_windows,
)
from sqlalchemy import text

from db import get_session

REACTION_TABLES = ("event_reaction_windows", "market_data", "market_events")

CONFIG: dict = {}


def setUpModule():
    url = pg_support.require_postgres()
    CONFIG.update(pg_support.parse_config(url))
    pg_support.provision(CONFIG)


class ReactionSqlIntegrationTests(unittest.TestCase):
    def setUp(self):
        pg_support.truncate(CONFIG, REACTION_TABLES)
        self.event_id = uuid4()
        self.event_at = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)  # Thursday
        self.config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "volatility_lookback_minutes": 1440,
                "target_tolerance_minutes": 5,
            },
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "Consumer Price Index",
                    "instruments": ["EURUSD"],
                    "expected_sensitivity": {"EURUSD": "negative"},
                }
            },
        }

    def _insert_event(self, session, event_at=None, event_id=None):
        session.execute(
            text(
                """INSERT INTO market_events
                   (id, schema_version, event_type, source, observed_at,
                    content_hash, dedupe_key, correlation_id, payload)
                   VALUES (:id, 1, 'macro_release', 'fred', :observed_at,
                           :hash, :dedupe, :correlation,
                           '{"series_id":"CPI"}'::jsonb)"""
            ),
            {
                "id": event_id or self.event_id,
                "observed_at": event_at or self.event_at,
                "hash": uuid4().hex,
                "dedupe": f"cpi-{uuid4().hex[:8]}",
                "correlation": uuid4(),
            },
        )
        session.commit()

    def _insert_market(self, session, rows):
        for row in rows:
            session.execute(
                text(
                    """INSERT INTO market_data
                       (symbol, timeframe, timestamp, open, high, low, close,
                        volume, source)
                       VALUES (:symbol, :timeframe, :timestamp, :open, :high,
                               :low, :close, NULL, :source)"""
                ),
                {
                    "symbol": row["symbol"],
                    "timeframe": row.get("timeframe", "PRICE"),
                    "timestamp": row["timestamp"],
                    "open": row.get("open", row["close"]),
                    "high": row.get("high", row["close"]),
                    "low": row.get("low", row["close"]),
                    "close": row["close"],
                    "source": row.get("source", "oanda"),
                },
            )
        session.commit()

    def _event(self, event_at, event_id=None):
        return {
            "event_id": event_id or self.event_id,
            "observed_at": event_at,
            "payload": {"series_id": "CPI"},
            "source_event_id": "source-1",
        }

    def test_more_than_100_pre_rows_direct_baseline_and_exact_event_exclusion(self):
        # 300 dense pre-event rows: the baseline must be the direct latest row
        # strictly before the event, not a range/limit artifact, and the row
        # exactly at the event timestamp must be excluded.
        with get_session(CONFIG) as session:
            self._insert_event(session)
            market = []
            for index in range(300):
                ts = self.event_at - timedelta(minutes=5) + timedelta(seconds=index)
                market.append(
                    {"symbol": "EURUSD", "timestamp": ts, "close": 100.0 + index * 0.01}
                )
            market.append(
                {"symbol": "EURUSD", "timestamp": self.event_at, "close": 999.0}
            )
            self._insert_market(session, market)
            summary = initialize_reaction_windows(
                session, self._event(self.event_at), self.config
            )
            self.assertEqual(summary["created"], 6)
            rows = list_event_reactions(session, self.event_id, limit=10)
            baseline_row = next(row for row in rows if row["horizon"] == "1m")
            self.assertEqual(
                baseline_row["baseline_at"],
                self.event_at - timedelta(seconds=1),
            )
            self.assertAlmostEqual(baseline_row["baseline_price"], 102.99)
            self.assertNotEqual(baseline_row["baseline_price"], 999.0)

    def test_more_than_500_post_rows_before_long_target(self):
        # 620 post-event rows at 3s cadence (31 minutes): the target query must
        # find the first row at/after the 30-minute target even though the
        # bounded path classification window caps at 500 rows.
        with get_session(CONFIG) as session:
            self._insert_event(session)
            market = [
                {
                    "symbol": "EURUSD",
                    "timestamp": self.event_at - timedelta(minutes=1),
                    "close": 100.0,
                }
            ]
            for index in range(620):
                ts = self.event_at + timedelta(seconds=3 * (index + 1))
                market.append({"symbol": "EURUSD", "timestamp": ts, "close": 99.0})
            self._insert_market(session, market)
            summary = initialize_reaction_windows(
                session, self._event(self.event_at), self.config
            )
            self.assertEqual(summary["created"], 6)
            target_at = self.event_at + timedelta(minutes=30)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=10),
                limit=50,
            )
            self.assertEqual(backfill["completed"], 4)
            self.assertEqual(backfill["unresolved"], 0)
            rows = list_event_reactions(session, self.event_id, limit=10)
            target_row = next(row for row in rows if row["horizon"] == "30m")
            self.assertEqual(target_row["observed_at"], target_at)
            self.assertEqual(target_row["target_price"], 99.0)
            self.assertIsNone(target_row["missing_data_reason"])

    def test_tolerance_boundary_in_and_out(self):
        # In: a target row exactly at target_at + tolerance resolves.
        event_in = uuid4()
        with get_session(CONFIG) as session:
            self._insert_event(session, event_id=event_in)
            target_at = self.event_at + timedelta(minutes=1)
            self._insert_market(
                session,
                [
                    {
                        "symbol": "EURUSD",
                        "timestamp": self.event_at - timedelta(minutes=1),
                        "close": 100.0,
                    },
                    # Exactly at the tolerance upper bound.
                    {
                        "symbol": "EURUSD",
                        "timestamp": target_at + timedelta(minutes=5),
                        "close": 101.0,
                    },
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(self.event_at, event_id=event_in), self.config
            )
            self.assertEqual(summary["created"], 6)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=10),
                limit=50,
            )
            self.assertEqual(backfill["unresolved"], 0)
        # Out: only a row beyond the tolerance bound yields missing_target.
        pg_support.truncate(CONFIG, REACTION_TABLES)
        event_out = uuid4()
        with get_session(CONFIG) as session:
            self._insert_event(session, event_id=event_out)
            target_at = self.event_at + timedelta(minutes=1)
            self._insert_market(
                session,
                [
                    {
                        "symbol": "EURUSD",
                        "timestamp": self.event_at - timedelta(minutes=1),
                        "close": 100.0,
                    },
                    {
                        "symbol": "EURUSD",
                        "timestamp": target_at + timedelta(minutes=5, seconds=1),
                        "close": 101.0,
                    },
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(self.event_at, event_id=event_out), self.config
            )
            self.assertEqual(summary["created"], 6)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=10),
                limit=50,
            )
            self.assertEqual(backfill["completed"], 1)
            self.assertEqual(backfill["unresolved"], 1)
            rows = list_event_reactions(session, event_out, limit=10)
            one_minute = next(row for row in rows if row["horizon"] == "1m")
            self.assertEqual(one_minute["missing_data_reason"], "missing_target")

    def test_stale_baseline_is_recorded_not_reselected(self):
        # The direct-latest baseline sits 3 hours before the event: the
        # 120-minute freshness policy marks the window stale_baseline instead
        # of silently accepting or silently skipping it.
        with get_session(CONFIG) as session:
            self._insert_event(session)
            stale_at = self.event_at - timedelta(hours=3)
            self._insert_market(
                session,
                [
                    {"symbol": "EURUSD", "timestamp": stale_at, "close": 100.0},
                    {"symbol": "EURUSD", "timestamp": self.event_at, "close": 101.0},
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(self.event_at), self.config
            )
            self.assertEqual(summary["created"], 6)
            target_at = self.event_at + timedelta(minutes=1)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=1),
                limit=50,
            )
            self.assertEqual(backfill["unresolved"], 1)
            rows = list_event_reactions(session, self.event_id, limit=10)
            one_minute = next(row for row in rows if row["horizon"] == "1m")
            self.assertEqual(one_minute["missing_data_reason"], "stale_baseline")
            self.assertEqual(one_minute["baseline_at"], stale_at)

    def test_eos_target_selects_pre_close_tick_negative_offset(self):
        # Friday event; venue close Friday 21:00. With no exact-close tick and
        # ticks at close-1m and close+1m, the eos target must be the close-1m
        # tick (post-close data never used) with a negative target offset.
        event_at = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)  # Friday
        with get_session(CONFIG) as session:
            self._insert_event(session, event_at=event_at)
            self._insert_market(
                session,
                [
                    {
                        "symbol": "EURUSD",
                        "timestamp": event_at - timedelta(minutes=1),
                        "close": 100.0,
                    },
                    {
                        "symbol": "EURUSD",
                        "timestamp": event_at + timedelta(minutes=59),
                        "close": 99.0,
                    },
                    {
                        "symbol": "EURUSD",
                        "timestamp": event_at + timedelta(minutes=61),
                        "close": 98.0,
                    },
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(event_at), self.config
            )
            self.assertEqual(summary["created"], 6)
            target_at = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=5),
                limit=50,
            )
            self.assertEqual(backfill["completed"], 2)
            rows = list_event_reactions(session, self.event_id, limit=10)
            eos = next(row for row in rows if row["horizon"] == "end_of_session")
            self.assertEqual(eos["observed_at"], target_at - timedelta(minutes=1))
            self.assertEqual(eos["target_price"], 99.0)
            self.assertEqual(eos["target_offset_seconds"], -60)
            self.assertIsNone(eos["missing_data_reason"])

    def test_friday_targets_never_cross_into_monday(self):
        # Neither the eos close-backward window nor the intraday wall-clock
        # tolerance may accept Monday data for a Friday target.
        event_at = datetime(2026, 8, 7, 20, 58, tzinfo=UTC)  # Friday
        monday_tick = datetime(2026, 8, 10, 0, 2, tzinfo=UTC)
        with get_session(CONFIG) as session:
            self._insert_event(session, event_at=event_at)
            self._insert_market(
                session,
                [
                    # Baseline sits outside the eos backward tolerance band
                    # ([20:55, 21:00]) so only the Monday tick could satisfy it.
                    {
                        "symbol": "EURUSD",
                        "timestamp": event_at - timedelta(minutes=8),
                        "close": 100.0,
                    },
                    {"symbol": "EURUSD", "timestamp": monday_tick, "close": 99.0},
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(event_at), self.config
            )
            self.assertEqual(summary["created"], 6)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=monday_tick + timedelta(minutes=1),
                limit=50,
            )
            self.assertEqual(backfill["unresolved"], 6)
            rows = list_event_reactions(session, self.event_id, limit=10)
            for row in rows:
                self.assertEqual(
                    row["missing_data_reason"], "missing_target", row["horizon"]
                )
                self.assertIsNone(row["observed_price"], row["horizon"])

    def test_multi_timeframe_uniqueness_and_identity(self):
        # Two timeframes for the same event/symbol/horizon coexist under the
        # 044 identity; a repeat initialize conflicts on the full key.
        config = {
            "oanda": {"snapshot_timeframe": "PRICE"},
            "reaction_windows": {
                "baseline_lookback_minutes": 120,
                "target_tolerance_minutes": 5,
            },
            "macro_event_mappings": {
                "CPI": {
                    "event_name": "CPI",
                    "instruments": [
                        {"symbol": "EURUSD", "timeframe": "PRICE"},
                        {"symbol": "EURUSD", "timeframe": "5m"},
                    ],
                    "expected_sensitivity": {"EURUSD": "negative"},
                }
            },
        }
        with get_session(CONFIG) as session:
            self._insert_event(session)
            self._insert_market(
                session,
                [
                    {
                        "symbol": "EURUSD",
                        "timestamp": self.event_at - timedelta(minutes=1),
                        "close": 100.0,
                    },
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(self.event_at), config
            )
            self.assertEqual(summary["created"], 12)
            again = initialize_reaction_windows(
                session, self._event(self.event_at), config
            )
            self.assertEqual(again["created"], 0)
            self.assertEqual(again["existing"], 12)
            rows = list_event_reactions(session, self.event_id, limit=20)
            self.assertEqual(len(rows), 12)
            self.assertEqual(len({(r["timeframe"], r["horizon"]) for r in rows}), 12)

    def test_legacy_recompute_relabels_only_v2(self):
        # Resolve one window normally (volatility_version=2), then insert a
        # legacy pre-044 row (volatility_version NULL) with observed data.
        # legacy-only recompute must touch only the legacy row, and dry-run
        # must not mutate anything.
        with get_session(CONFIG) as session:
            self._insert_event(session)
            target_at = self.event_at + timedelta(minutes=1)
            self._insert_market(
                session,
                [
                    {
                        "symbol": "EURUSD",
                        "timestamp": self.event_at - timedelta(minutes=1),
                        "close": 100.0,
                    },
                    {"symbol": "EURUSD", "timestamp": target_at, "close": 99.0},
                    {
                        "symbol": "EURUSD",
                        "timeframe": "5m",
                        "timestamp": self.event_at - timedelta(minutes=1),
                        "close": 100.0,
                    },
                    {
                        "symbol": "EURUSD",
                        "timeframe": "5m",
                        "timestamp": target_at,
                        "close": 99.0,
                    },
                ],
            )
            summary = initialize_reaction_windows(
                session, self._event(self.event_at), self.config
            )
            self.assertEqual(summary["created"], 6)
            backfill = backfill_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=1),
                limit=50,
            )
            self.assertEqual(backfill["completed"], 1)
            # Fabricate a legacy resolved row (pre-044: NULL volatility_version).
            session.execute(
                text(
                    """INSERT INTO event_reaction_windows
                       (event_id, instrument_symbol, timeframe, horizon, event_at,
                        baseline_at, target_at, baseline_price, target_price,
                        observed_at, observed_price, percentage_move,
                        expected_direction, sensitivity, reaction_state,
                        volatility_version, provenance)
                       VALUES (:event_id, 'EURUSD', '5m', '1m',
                               :event_at, :baseline_at, :target_at, 100.0, 99.0,
                               :target_at, 99.0, -1.0, 'neutral', 'neutral',
                               'persistence', NULL, '{}'::jsonb)"""
                ),
                {
                    "event_id": self.event_id,
                    "event_at": self.event_at,
                    "baseline_at": self.event_at - timedelta(minutes=1),
                    "target_at": target_at,
                },
            )
            session.commit()
            # Dry run: reports the legacy row without mutating.
            dry = recompute_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=1),
                limit=50,
                legacy_only=True,
                dry_run=True,
            )
            self.assertEqual(dry["scanned"], 1)
            self.assertEqual(dry["dry_run"], True)
            row = session.execute(
                text(
                    """SELECT volatility_version FROM event_reaction_windows
                       WHERE timeframe = '5m' AND horizon = '1m'"""
                )
            ).scalar()
            self.assertIsNone(row)
            # Real run: the legacy row is relabeled to the current version.
            real = recompute_reaction_windows(
                session,
                self.config,
                now=target_at + timedelta(minutes=1),
                limit=50,
                legacy_only=True,
            )
            self.assertEqual(real["scanned"], 1)
            self.assertEqual(real["completed"], 1)
            row = session.execute(
                text(
                    """SELECT volatility_version FROM event_reaction_windows
                       WHERE timeframe = '5m' AND horizon = '1m'"""
                )
            ).scalar()
            self.assertEqual(row, 2)


if __name__ == "__main__":
    unittest.main()
