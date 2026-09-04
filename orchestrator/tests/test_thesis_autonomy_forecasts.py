"""Tests for thesis autonomy forecast generation, backfill cutoffs, and outcome resolution."""

import sys
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from thesis_autonomy_support import (
    EXISTING_ID,
    NOW,
    MemorySession,
    _resolve_matured_forecasts,
)


class ForecastTests(unittest.TestCase):
    def test_matured_forecasts_resolve_once_from_point_in_time_prices(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        session.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0),
            (boundary - timedelta(hours=8), 100.0),
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(counts["miss"], 0)
        self.assertEqual(counts["inconclusive"], 0)
        self.assertEqual(len(session.outcomes), 1)
        # The fake retains the measured values for point-in-time assertions.
        outcome = session.outcomes["44444444-4444-4444-8444-444444444444"]
        self.assertEqual(outcome["status"], "hit")
        self.assertEqual(outcome["actual_value"], 110.0)
        self.assertEqual(outcome["measured_at"], NOW)
        # Outcomes are recorded once and never overwritten.
        again = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(again["hit"], 0)
        self.assertEqual(len(session.outcomes), 1)

    def test_miss_inconclusive_and_open_outcomes(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 90.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["miss"], 1)

        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="NOPRICE")
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=NOW.date() - timedelta(days=30),
        )
        counts = _resolve_matured_forecasts(session2, NOW)
        self.assertEqual(counts["inconclusive"], 1)

        session3 = MemorySession()
        session3.seed_thesis(EXISTING_ID, symbol="NOPRICE")
        session3.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=NOW.date() - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session3, NOW)
        self.assertEqual(counts["open"], 1)
        self.assertEqual(session3.outcomes, {})

    def test_run_before_target_boundary_never_resolves(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=NOW.date(),  # boundary (end of today UTC) not reached
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

    def test_resolution_uses_the_target_boundary_close_not_later_bars(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        session.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0),  # terminal close at boundary
            (boundary + timedelta(hours=2), 95.0),  # post-boundary bar: never used
            (NOW + timedelta(days=2), 80.0),  # much later bar: never used
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(counts["miss"], 0)
        outcome = session.outcomes["44444444-4444-4444-8444-444444444444"]
        self.assertEqual(outcome["actual_value"], 110.0)
        self.assertEqual(outcome["measured_at"], NOW)
        # A delayed run (days later) still measures the same boundary close
        # and never records a second outcome.
        delayed = _resolve_matured_forecasts(session, NOW + timedelta(days=5))
        self.assertEqual(delayed["hit"], 0)
        self.assertEqual(delayed["miss"], 0)
        self.assertEqual(len(session.outcomes), 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["actual_value"],
            110.0,
        )

    def test_weekend_target_uses_the_prior_available_close(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        saturday = date(2026, 8, 8)  # a Saturday
        session.market_data["ACME"] = [
            (datetime(2026, 8, 7, 21, 0, tzinfo=UTC), 100.0),  # Friday close
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=saturday,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["miss"], 1)  # 100 < 105 on an up forecast
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["actual_value"],
            100.0,
        )

    def test_bars_unavailable_at_replay_time_are_excluded(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=30)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        # Timestamped at/before the boundary, but only ingested AFTER the
        # replay cutoff: a replay run must not see it.
        session.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0, NOW + timedelta(minutes=5))
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["inconclusive"], 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["status"],
            "inconclusive",
        )

        # Control: the same bar ingested before the cutoff is eligible.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [
            (boundary - timedelta(hours=4), 110.0, NOW - timedelta(days=31))
        ]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session2, NOW)
        self.assertEqual(counts["hit"], 1)

    def test_bars_revised_after_replay_time_are_excluded(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        target_day = NOW.date() - timedelta(days=30)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)
        # Ingested before the replay cutoff but REVISED after it (any row
        # mutation bumps updated_at): a replay run must not see the bar
        # even though its created_at predates the cutoff.
        session.market_data["ACME"] = [
            (
                boundary - timedelta(hours=4),
                110.0,
                NOW - timedelta(days=31),
                NOW + timedelta(minutes=5),
            )
        ]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["inconclusive"], 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["status"],
            "inconclusive",
        )

        # Control: ingested and last revised before the cutoff stays
        # eligible and resolves exactly like the created_at-only control.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [
            (
                boundary - timedelta(hours=4),
                110.0,
                NOW - timedelta(days=31),
                NOW - timedelta(days=31),
            )
        ]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
        )
        counts = _resolve_matured_forecasts(session2, NOW)
        self.assertEqual(counts["hit"], 1)

    def test_resolver_owns_price_forecasts_only(self):
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        boundary = datetime.combine(
            NOW.date() - timedelta(days=1), time.max, tzinfo=UTC
        )
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            forecast_type="earnings",
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

        # A matured price forecast is resolved alongside the non-price one,
        # which stays open for its domain-specific resolver.
        session.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            forecast_type="price",
        )
        counts = _resolve_matured_forecasts(session, NOW)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(
            set(session.outcomes), {"55555555-5555-4555-8555-555555555555"}
        )

    def test_resolver_excludes_forecasts_created_or_frozen_after_reference(self):
        # A historical replay must see exactly the forecasts that existed at
        # its cutoff: a forecast persisted (created_at) or frozen (as_of)
        # after the reference is invisible, even if it is current today.
        reference = NOW - timedelta(days=2)
        target_day = reference.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)

        # Created after the reference (but before today): excluded.
        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference - timedelta(days=1),
            created_at=reference + timedelta(hours=1),
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

        # Frozen (as_of) after the reference: excluded the same way.
        session.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference + timedelta(hours=1),
            created_at=reference - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session.outcomes, {})

        # Control: the same forecasts visible at the reference resolve once.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference - timedelta(days=1),
            created_at=reference - timedelta(days=1),
        )
        session2.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            as_of=reference - timedelta(days=1),
            created_at=reference - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session2, reference)
        self.assertEqual(counts["hit"], 2)
        self.assertEqual(len(session2.outcomes), 2)

    def test_resolver_treats_superseded_at_point_in_time(self):
        # A forecast superseded AFTER the reference was still active at the
        # reference and must resolve; superseded on/before the reference it
        # was already inactive and must not.
        reference = NOW - timedelta(days=2)
        target_day = reference.date() - timedelta(days=1)
        boundary = datetime.combine(target_day, time.max, tzinfo=UTC)

        session = MemorySession()
        session.seed_thesis(EXISTING_ID, symbol="ACME")
        session.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            superseded_at=reference + timedelta(days=1),  # active at reference
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts["hit"], 1)
        self.assertEqual(
            session.outcomes["44444444-4444-4444-8444-444444444444"]["status"],
            "hit",
        )

        # Superseded exactly at the reference: no longer active.
        session2 = MemorySession()
        session2.seed_thesis(EXISTING_ID, symbol="ACME")
        session2.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session2.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            superseded_at=reference,
        )
        counts = _resolve_matured_forecasts(session2, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session2.outcomes, {})

        # Superseded before the reference: excluded as well.
        session3 = MemorySession()
        session3.seed_thesis(EXISTING_ID, symbol="ACME")
        session3.market_data["ACME"] = [(boundary - timedelta(hours=4), 110.0)]
        session3.seed_forecast(
            "44444444-4444-4444-8444-444444444444",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            superseded_at=reference - timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session3, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(session3.outcomes, {})

        # A non-price forecast is untouched by all of this: even superseded
        # after the reference, it stays open for its domain-specific
        # resolver and records nothing here.
        session.seed_forecast(
            "55555555-5555-4555-8555-555555555555",
            direction="up",
            target_value=105.0,
            target_date=target_day,
            forecast_type="earnings",
            superseded_at=reference + timedelta(days=1),
        )
        counts = _resolve_matured_forecasts(session, reference)
        self.assertEqual(counts, {"hit": 0, "miss": 0, "inconclusive": 0, "open": 0})
        self.assertEqual(
            set(session.outcomes), {"44444444-4444-4444-8444-444444444444"}
        )


if __name__ == "__main__":
    unittest.main()
