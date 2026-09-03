"""Tests for thesis autonomy forecast generation, backfill cutoffs, and outcome resolution."""

import copy
import sys
import unittest
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

ORCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCH_ROOT))

from thesis_autonomy_support import (
    CANDIDATE,
    EXISTING_ID,
    NOW,
    MemorySession,
    NormalizedEntity,
    ScriptedAuditor,
    ScriptedChallenger,
    ScriptedRunner,
    _backfill_missing_forecasts,
    _id,
    _resolve_matured_forecasts,
    cycle_config,
    evidence_item,
    run_autonomous_thesis_cycle,
    run_cycle,
)

from research_intelligence.evidence import EvidenceCollection, EvidenceRegistry


class ForecastTests(unittest.TestCase):
    def test_missing_forecasts_backfill_after_market_price_arrives(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            company="Acme Corporation",
            symbol="ACME",
            input_fingerprint="f" * 64,
        )
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        for label, expected_return in (("bull", 0.1), ("base", 0.0), ("bear", -0.2)):
            session.scenarios.append(
                {
                    "id": _id(f"scenario:{label}"),
                    "thesis_id": EXISTING_ID,
                    "name": label,
                    "expected_return": expected_return,
                    "superseded_at": None,
                }
            )

        first = _backfill_missing_forecasts(session, NOW)
        second = _backfill_missing_forecasts(session, NOW)

        self.assertEqual(first, 3)
        self.assertEqual(second, 0)
        self.assertEqual(len(session.forecasts), 3)

    def test_forecasts_are_frozen_with_deterministic_targets(self):
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["forecasts_frozen"], 3)
        thesis_id = next(iter(session.theses))
        by_label = {}
        for row in session.forecasts:
            self.assertIsNone(row["superseded_at"])
            self.assertEqual(row["thesis_id"], thesis_id)
            self.assertEqual(row["forecast_type"], "price")
            self.assertEqual(row["as_of"], NOW)
            label = row["forecast_key"].split(":")[2]
            by_label[label] = row
        # Long thesis: target = close * (1 + fractional P&L).
        self.assertEqual(by_label["bull"]["direction"], "up")
        self.assertEqual(by_label["bull"]["target_value"], 110.0)
        self.assertEqual(by_label["base"]["direction"], "flat")
        self.assertEqual(by_label["base"]["target_value"], 100.0)
        self.assertEqual(by_label["bear"]["direction"], "down")
        self.assertEqual(by_label["bear"]["target_value"], 80.0)
        expected_date = NOW.date() + timedelta(days=90)
        for row in session.forecasts:
            self.assertEqual(row["target_date"], expected_date)
            self.assertTrue(row["forecast_key"].startswith(f"autonomy:{thesis_id}:"))
            scenario = next(
                s for s in session.scenarios if s["id"] == row["scenario_id"]
            )
            self.assertEqual(scenario["thesis_id"], thesis_id)

    def test_short_thesis_uses_the_inverse_factor(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["direction"] = "short"
        candidate["scenarios"] = {
            "bull": {
                "probability": 0.3,
                "expected_return": 0.2,
                "description": "competition fails and margins widen",
            },
            "base": {
                "probability": 0.5,
                "expected_return": 0.0,
                "description": "competition holds margins flat",
            },
            "bear": {
                "probability": 0.2,
                "expected_return": -0.2,
                "description": "competition erodes margins",
            },
        }
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(session, runner=ScriptedRunner(candidate))
        self.assertEqual(result["forecasts_frozen"], 3)
        by_label = {}
        for row in session.forecasts:
            by_label[row["forecast_key"].split(":")[2]] = row
        # Short thesis: target = close * (1 - fractional P&L); a bull leg is
        # a falling price.
        self.assertEqual(by_label["bull"]["direction"], "down")
        self.assertEqual(by_label["bull"]["target_value"], 80.0)
        self.assertEqual(by_label["base"]["direction"], "flat")
        self.assertEqual(by_label["base"]["target_value"], 100.0)
        self.assertEqual(by_label["bear"]["direction"], "up")
        self.assertEqual(by_label["bear"]["target_value"], 120.0)

    def test_neutral_thesis_and_invalid_extremes_skip_freezing(self):
        neutral = copy.deepcopy(CANDIDATE)
        neutral["direction"] = "neutral"
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result = run_cycle(session, runner=ScriptedRunner(neutral))
        self.assertEqual(result["forecasts_frozen"], 0)
        self.assertEqual(session.forecasts, [])

        # Long thesis with a fractional return at or below -1 has no
        # positive target; it stays unknown and is never clamped.
        extreme = copy.deepcopy(CANDIDATE)
        extreme["scenarios"]["bull"]["expected_return"] = -1.5
        extreme["scenarios"]["base"]["expected_return"] = -1.0
        extreme["scenarios"]["bear"]["expected_return"] = -0.2
        session2 = MemorySession()
        session2.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result2 = run_cycle(session2, runner=ScriptedRunner(extreme))
        self.assertEqual(result2["forecasts_frozen"], 1)
        self.assertEqual(len(session2.forecasts), 1)
        self.assertEqual(session2.forecasts[0]["target_value"], 80.0)

        # Short thesis with a return above +1 has a non-positive factor.
        short_extreme = copy.deepcopy(CANDIDATE)
        short_extreme["direction"] = "short"
        short_extreme["scenarios"]["bear"]["expected_return"] = 1.5
        session3 = MemorySession()
        session3.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        result3 = run_cycle(session3, runner=ScriptedRunner(short_extreme))
        self.assertEqual(result3["forecasts_frozen"], 2)

    def test_no_symbol_or_close_skips_freezing(self):
        session = MemorySession()
        result = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(result["forecasts_frozen"], 0)
        self.assertEqual(session.forecasts, [])

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

    def test_rerun_with_later_as_of_keeps_one_active_forecast_per_scenario(self):
        session = MemorySession()
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        first = run_cycle(session, runner=ScriptedRunner(CANDIDATE))
        self.assertEqual(first["forecasts_frozen"], 3)
        self.assertEqual(len(session.forecasts), 3)
        # A later rerun over the same scenarios must not freeze a second
        # active forecast: the first frozen as_of/close/target/date wins.
        second = run_cycle(
            session,
            runner=ScriptedRunner(CANDIDATE),
            as_of=NOW + timedelta(days=1),
        )
        self.assertEqual(second["forecasts_frozen"], 0)
        active = [row for row in session.forecasts if row["superseded_at"] is None]
        self.assertEqual(len(active), 3)
        self.assertEqual(len(session.forecasts), 3)
        for row in active:
            self.assertEqual(row["as_of"], NOW)

    def test_mixed_case_stored_symbol_still_freezes_forecasts(self):
        session = MemorySession()
        session.seed_thesis(
            EXISTING_ID,
            symbol=" acme ",
            direction="long",
            horizon="months",
        )
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        for label, expected_return in (("bull", 0.1), ("base", 0.0), ("bear", -0.2)):
            session.scenarios.append(
                {
                    "id": _id(f"scenario:{label}"),
                    "thesis_id": EXISTING_ID,
                    "name": label,
                    "expected_return": expected_return,
                    "superseded_at": None,
                }
            )
        frozen = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(frozen, 3)
        by_direction = {
            row["direction"]: row["target_value"] for row in session.forecasts
        }
        self.assertEqual(by_direction, {"up": 110.0, "flat": 100.0, "down": 80.0})

    def test_promoted_dotted_lowercase_symbol_is_canonicalized(self):
        candidate = copy.deepcopy(CANDIDATE)
        candidate["subject"] = "Berkshire Hathaway"
        candidate["instrument"] = "BRK.B"
        candidate["claim"] = "Berkshire Hathaway insurance float should compound"
        entities = [
            NormalizedEntity.create(
                "company",
                "berkshire-hathaway",
                "Berkshire Hathaway",
            ),
            # Mixed-case dotted display name: persisted canonical.
            NormalizedEntity.create("symbol", "brk-b", "Brk.B"),
        ]
        items = [evidence_item(index, entities=entities) for index in range(3)]
        session = MemorySession()
        session.market_data["BRK.B"] = [(NOW - timedelta(hours=1), 100.0)]
        with patch.object(
            EvidenceRegistry,
            "collect",
            return_value=EvidenceCollection(items=tuple(items), failures={}),
        ):
            result = run_autonomous_thesis_cycle(
                session,
                cycle_config(),
                as_of=NOW,
                runner=ScriptedRunner(candidate),
                challenger=ScriptedChallenger(),
                auditor=ScriptedAuditor(),
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["forecasts_frozen"], 3)
        thesis = next(iter(session.theses.values()))
        self.assertEqual(thesis["symbol"], "BRK.B")
        self.assertEqual(thesis["company"], "Berkshire Hathaway")
        for row in session.forecasts:
            self.assertIsNone(row["superseded_at"])
            self.assertEqual(row["as_of"], NOW)


class ForecastBackfillCutoffTests(unittest.TestCase):
    """Backfill consumes only thesis/scenario state visible at the reference.

    A historical or delayed run must never backdate a forecast for thesis
    or scenario state that did not exist at its accepted reference, while
    point-in-time visible legacy scenarios still backfill once prices
    arrive and live idempotency and bounds stay intact.
    """

    def _seed(self, session, **thesis_overrides) -> MemorySession:
        session.seed_thesis(
            EXISTING_ID,
            company="Acme Corporation",
            symbol="ACME",
            input_fingerprint="f" * 64,
            **thesis_overrides,
        )
        session.market_data["ACME"] = [(NOW - timedelta(hours=1), 100.0)]
        for label, expected_return in (("bull", 0.1), ("base", 0.0), ("bear", -0.2)):
            session.scenarios.append(
                {
                    "id": _id(f"scenario:{label}"),
                    "thesis_id": EXISTING_ID,
                    "name": label,
                    "expected_return": expected_return,
                    "created_at": NOW - timedelta(days=2),
                    "superseded_at": None,
                }
            )
        return session

    def test_thesis_created_after_cutoff_is_not_backfilled(self):
        session = self._seed(MemorySession(), created_at=NOW + timedelta(hours=1))
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 0)
        self.assertEqual(session.forecasts, [])

    def test_thesis_updated_after_cutoff_is_not_backfilled(self):
        session = self._seed(MemorySession(), updated_at=NOW + timedelta(hours=1))
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 0)
        self.assertEqual(session.forecasts, [])

    def test_fusion_reference_after_cutoff_is_not_backfilled(self):
        # A thesis whose accepted fusion reference postdates the cutoff was
        # not accepted-fusion content at the reference: no forecast.
        session = self._seed(
            MemorySession(), fusion_reference_at=NOW + timedelta(hours=1)
        )
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 0)
        self.assertEqual(session.forecasts, [])

    def test_scenario_created_after_cutoff_is_not_backfilled(self):
        session = self._seed(MemorySession())
        late = session.scenarios[2]
        late["created_at"] = NOW + timedelta(hours=1)
        frozen = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(frozen, 2)
        self.assertFalse(
            any(row["scenario_id"] == late["id"] for row in session.forecasts)
        )
        visible = session.scenarios[:2]
        self.assertEqual(
            {row["scenario_id"] for row in session.forecasts},
            {s["id"] for s in visible},
        )

    def test_scenario_superseded_after_cutoff_is_still_visible(self):
        # A legacy scenario only superseded later is point-in-time visible
        # at the reference and backfills once price data arrives.
        session = self._seed(MemorySession())
        for scenario in session.scenarios:
            scenario["superseded_at"] = NOW + timedelta(days=1)
        self.assertEqual(_backfill_missing_forecasts(session, NOW), 3)
        self.assertEqual(len(session.forecasts), 3)

    def test_scenario_superseded_on_or_before_cutoff_is_excluded(self):
        session = self._seed(MemorySession())
        session.scenarios[0]["superseded_at"] = NOW - timedelta(days=1)
        session.scenarios[1]["superseded_at"] = NOW  # exactly on the cutoff
        frozen = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(frozen, 1)
        active = [s for s in session.scenarios if s["superseded_at"] is None]
        self.assertEqual(
            [row["scenario_id"] for row in session.forecasts], [active[0]["id"]]
        )

    def test_current_visible_row_still_backfills_and_stays_idempotent(self):
        # A fully visible current row (fusion reference in the past or NULL,
        # created/updated before the cutoff, active scenarios) still
        # backfills exactly once; a rerun stays a no-op.
        session = self._seed(
            MemorySession(), fusion_reference_at=NOW - timedelta(days=1)
        )
        first = _backfill_missing_forecasts(session, NOW)
        second = _backfill_missing_forecasts(session, NOW)
        self.assertEqual(first, 3)
        self.assertEqual(second, 0)
        self.assertEqual(len(session.forecasts), 3)

    def test_forecast_active_at_cutoff_then_superseded_blocks_replay(self):
        # A forecast frozen before the reference and only superseded after
        # it was ACTIVE at the reference: a later replay must not re-freeze
        # the scenario (the original run at the reference saw the forecast
        # and froze nothing), even though the row is no longer active
        # today.  The other scenarios are the visible control and still
        # backfill exactly once.
        reference = NOW - timedelta(days=2)
        session = self._seed(
            MemorySession(),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.market_data["ACME"] = [(reference - timedelta(hours=1), 100.0)]
        session.seed_forecast(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id=_id("scenario:bull"),
            as_of=reference - timedelta(days=1),
            superseded_at=reference + timedelta(days=1),
        )
        seeded_ids = {row["id"] for row in session.forecasts}
        frozen = _backfill_missing_forecasts(session, reference)
        appended = [row for row in session.forecasts if row["id"] not in seeded_ids]
        self.assertEqual(frozen, 2)
        # Only base/bear were frozen by the replay; no duplicate bull row.
        self.assertEqual(
            {row["scenario_id"] for row in appended},
            {_id("scenario:base"), _id("scenario:bear")},
        )
        # The superseded bull forecast stays exactly as seeded (immutable
        # history, never re-frozen at the reference).
        self.assertEqual(
            [
                row["id"]
                for row in session.forecasts
                if row["scenario_id"] == _id("scenario:bull")
            ],
            ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
        )

    def test_forecast_superseded_on_or_before_cutoff_does_not_block_replay(self):
        # A forecast already superseded on/before the reference was not
        # active at it; the scenario legitimately has no forecast at the
        # reference and backfills once (same point-in-time boundary as the
        # scenario supersede guard).
        reference = NOW - timedelta(days=2)
        session = self._seed(
            MemorySession(),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.market_data["ACME"] = [(reference - timedelta(hours=1), 100.0)]
        session.seed_forecast(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id=_id("scenario:bull"),
            as_of=reference - timedelta(days=1),
            superseded_at=reference - timedelta(days=1),
        )
        seeded = len(session.forecasts)
        self.assertEqual(_backfill_missing_forecasts(session, reference), 3)
        # Exactly the three new forecast rows were appended; the seeded
        # historical row is untouched.
        self.assertEqual(len(session.forecasts) - seeded, 3)

    def test_forecast_frozen_after_cutoff_does_not_block_replay(self):
        # A forecast frozen after the reference did not exist at it (even
        # though it is superseded today), so the scenario backfills exactly
        # as the original run at the reference would have.
        reference = NOW - timedelta(days=2)
        session = self._seed(
            MemorySession(),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        session.market_data["ACME"] = [(reference - timedelta(hours=1), 100.0)]
        session.seed_forecast(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            scenario_id=_id("scenario:bull"),
            as_of=reference + timedelta(days=1),
            superseded_at=reference + timedelta(days=2),
        )
        seeded = len(session.forecasts)
        self.assertEqual(_backfill_missing_forecasts(session, reference), 3)
        # Exactly the three new forecast rows were appended; the seeded
        # historical row is untouched.
        self.assertEqual(len(session.forecasts) - seeded, 3)


if __name__ == '__main__':
    unittest.main()
