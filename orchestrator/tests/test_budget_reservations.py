"""Budget reservation admission: unit contract + real-PostgreSQL concurrency.

The unit tests mock the session answering the admission statement sequence.
The PostgreSQL tests prove the transactional guarantee on a real database:
concurrent workers cannot oversubscribe ``spent + active reservations +
estimate`` beyond the daily cap, and expired reservations release their funds.
Set ``TEST_DATABASE_URL`` to a disposable PostgreSQL database to enable them.
"""

import math
import sys
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pg_support import parse_config, provision, require_postgres, truncate  # noqa: E402

from budgets import (
    BudgetExceeded,
    BudgetUnavailable,
    _known_free_model,
    _reservation_policy,
    _reserve_budget_quota,
    enforce_budget,
    expire_abandoned_reservations,
    release_budget_reservation,
    retain_budget_reservation,
    settle_budget_reservation,
)
from db import get_engine  # noqa: E402

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _session(rows, insert_id="reservation-1"):
    """Mock session answering lock, sweep, sums, then optional insert."""
    session = Mock()
    lock = Mock()
    lock.fetchone.return_value = None
    sweep = Mock()
    sweep.fetchone.return_value = None
    session.execute.side_effect = [lock, sweep, *rows]
    return session


def _sums(spent, reserved):
    sums = Mock()
    sums.fetchone.return_value = SimpleNamespace(
        _mapping={"spent_usd": spent, "reserved_usd": reserved}
    )
    return sums


def _insert(result):
    insert = Mock()
    insert.fetchone.return_value = result
    return insert


class ReservationPolicyTests(unittest.TestCase):
    def test_paid_model_without_configured_pricing_fails_closed(self):
        # A paid call whose pricing is not configured must never be admitted
        # with a guessed estimate: no estimate, no reservation, no dispatch.
        for budgets in ({}, {"daily_llm_usd": 2.0}):
            with self.subTest(budgets=budgets):
                with self.assertRaisesRegex(ValueError, "no configured pricing"):
                    _reservation_policy({"budgets": budgets}, "briefing")

    def test_generic_pricing_is_the_paid_fallback(self):
        config = {"budgets": {"reservation_estimate_usd": 0.05}}
        self.assertEqual(_reservation_policy(config, "briefing"), (0.05, 600.0))

    def test_per_processor_estimate_wins_over_global(self):
        config = {
            "budgets": {
                "reservation_estimate_usd": 0.5,
                "estimates": {"briefing": 0.2},
            }
        }
        self.assertEqual(_reservation_policy(config, "briefing")[0], 0.2)
        self.assertEqual(_reservation_policy(config, "macro_regime")[0], 0.5)

    def test_known_free_model_reserves_zero_without_pricing(self):
        # A known-free slug (OpenRouter :free variant) reserves zero cost
        # even when no pricing is configured at all.
        self.assertEqual(
            _reservation_policy(
                {}, "thesis_autonomy", model="nvidia/nemotron-3-super-120b-a12b:free"
            ),
            (0.0, 600.0),
        )

    def test_known_free_detection_is_case_and_whitespace_tolerant(self):
        self.assertTrue(_known_free_model("Provider/Model:free"))
        self.assertTrue(_known_free_model(" provider/model:FREE "))
        self.assertFalse(_known_free_model("provider/model"))
        self.assertFalse(_known_free_model("provider/model:freebie"))
        self.assertFalse(_known_free_model(None))
        self.assertFalse(_known_free_model(""))

    def test_invalid_policy_fails_closed(self):
        for budgets in (
            {"reservation_estimate_usd": 0},
            {"reservation_estimate_usd": -1},
            {"reservation_estimate_usd": math.nan},
            {"reservation_estimate_usd": "bad"},
            {"reservation_ttl_seconds": 0},
            {"estimates": {"briefing": math.inf}},
        ):
            with self.subTest(budgets=budgets), self.assertRaises(ValueError):
                _reservation_policy({"budgets": budgets}, "briefing")

    def test_ttl_gate_rejects_sub_deadline_ttl(self):
        # TTL must cover llm.stage_timeout_seconds * max_retries + 30s slack.
        config = {
            "llm": {"stage_timeout_seconds": 90, "max_retries": 1},
            "budgets": {
                "reservation_estimate_usd": 0.05,
                "reservation_ttl_seconds": 60,
            },
        }
        with self.assertRaises(ValueError):
            _reservation_policy(config, "briefing")
        config["budgets"]["reservation_ttl_seconds"] = 120
        self.assertEqual(_reservation_policy(config, "briefing")[1], 120.0)

    def test_short_ttl_denied_before_any_reservation_or_provider(self):
        config = {
            "llm": {"stage_timeout_seconds": 90, "max_retries": 1},
            "budgets": {
                "daily_llm_usd": 2.0,
                "reservation_estimate_usd": 0.05,
                "reservation_ttl_seconds": 60,
            },
        }
        with patch("budgets._reserve_budget_quota") as reserve:
            with self.assertRaises(BudgetUnavailable):
                enforce_budget(config, "briefing")
        reserve.assert_not_called()


class ReservationAdmissionTests(unittest.TestCase):
    def test_reserves_when_quota_available(self):
        session = _session([_sums(1.0, 0.25), _insert(("res-1",))])
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            reservation_id = _reserve_budget_quota(
                {},
                "briefing",
                cap=2.0,
                estimate_usd=0.5,
                ttl_seconds=600,
                correlation_id="cid-1",
                run_kind="processor",
                component="briefing",
            )
        self.assertEqual(reservation_id, "res-1")
        insert_sql = str(session.execute.call_args_list[3].args[0])
        self.assertIn("INSERT INTO budget_reservations", insert_sql)
        insert_params = session.execute.call_args_list[3].args[1]
        self.assertEqual(insert_params["processor"], "briefing")
        self.assertEqual(insert_params["estimate"], 0.5)
        self.assertEqual(insert_params["cid"], "cid-1")
        self.assertEqual(insert_params["run_kind"], "processor")
        self.assertEqual(insert_params["component"], "briefing")

    def test_blocks_when_spent_plus_reserved_plus_estimate_over_cap(self):
        session = _session([_sums(1.6, 0.25)])
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            with self.assertRaises(BudgetExceeded) as raised:
                _reserve_budget_quota(
                    {}, "briefing", cap=2.0, estimate_usd=0.5, ttl_seconds=600
                )
        self.assertAlmostEqual(raised.exception.today_cost, 1.85)
        self.assertEqual(raised.exception.cap, 2.0)
        # No insert was attempted on the blocked path.
        self.assertEqual(len(session.execute.call_args_list), 3)

    def test_exact_cap_is_admitted(self):
        session = _session([_sums(1.5, 0.0), _insert(("res-1",))])
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            reservation_id = _reserve_budget_quota(
                {}, "briefing", cap=2.0, estimate_usd=0.5, ttl_seconds=600
            )
        self.assertEqual(reservation_id, "res-1")

    def test_malformed_spend_fails_closed(self):
        for spent in (None, math.nan, math.inf, "garbage"):
            with self.subTest(spent=spent):
                session = _session([_sums(spent, 0.0)])
                with patch("budgets.get_session") as get_session:
                    get_session.return_value.__enter__.return_value = session
                    with self.assertRaises(Exception):
                        _reserve_budget_quota(
                            {}, "briefing", cap=2.0, estimate_usd=0.5,
                            ttl_seconds=600,
                        )

    def test_enforce_budget_maps_unreadable_budget_to_unavailable(self):
        with patch(
            "budgets.get_session",
            side_effect=RuntimeError("connection refused"),
        ):
            with self.assertRaises(BudgetUnavailable):
                enforce_budget(
                    {
                        "budgets": {
                            "daily_llm_usd": 2.0,
                            "reservation_estimate_usd": 0.05,
                        }
                    },
                    "briefing",
                )

    def test_enforce_budget_rejects_paid_model_with_unknown_pricing(self):
        # A paid model with no configured pricing fails closed into
        # BudgetUnavailable before any reservation or provider call.
        with patch("budgets._reserve_budget_quota") as reserve:
            with self.assertRaises(BudgetUnavailable):
                enforce_budget(
                    {"budgets": {"daily_llm_usd": 2.0}},
                    "thesis_autonomy",
                    model="openai/gpt-5.6-luna",
                )
        reserve.assert_not_called()

    def test_enforce_budget_admits_known_free_model_with_zero_reservation(self):
        # A known-free model reserves zero cost and is admitted even when the
        # paid cap is fully committed: the reservation cannot oversubscribe.
        session = _session([_sums(2.0, 0.0), _insert(("res-free",))])
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            permit = enforce_budget(
                {
                    "budgets": {
                        "daily_llm_usd": 2.0,
                        "reservation_estimate_usd": 0.05,
                    }
                },
                "thesis_autonomy",
                model="nvidia/nemotron-3-super-120b-a12b:free",
                correlation_id="cid-free",
                component="thesis_autonomy",
            )
        self.assertTrue(permit.valid)
        self.assertEqual(permit.reservation_id, "res-free")
        insert_params = session.execute.call_args_list[3].args[1]
        self.assertEqual(insert_params["estimate"], 0.0)
        self.assertEqual(insert_params["processor"], "thesis_autonomy")
        # The exhausted cap was bypassed for the zero-cost reservation: the
        # sums query still ran (spent 2.0, reserved 0.0) but no BudgetExceeded
        # was raised and the insert happened.
        self.assertEqual(len(session.execute.call_args_list), 4)

    def test_zero_cap_denies_all_paid_calls(self):
        # A zero daily cap denies every paid call. With pricing configured
        # the quota check rejects the reservation outright...
        session = _session([_sums(0.0, 0.0)])
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            with self.assertRaises(BudgetExceeded) as raised:
                enforce_budget(
                    {
                        "budgets": {
                            "daily_llm_usd": 0,
                            "reservation_estimate_usd": 0.05,
                        }
                    },
                    "briefing",
                )
        self.assertEqual(raised.exception.cap, 0.0)
        # No insert was attempted on the blocked path.
        self.assertEqual(len(session.execute.call_args_list), 3)
        # ...and without pricing the paid call fails closed (BudgetUnavailable)
        # before any reservation is attempted, never admitting with a guess.
        with patch("budgets._reserve_budget_quota") as reserve:
            with self.assertRaises(BudgetUnavailable):
                enforce_budget({"budgets": {"daily_llm_usd": 0}}, "briefing")
        reserve.assert_not_called()

    def test_known_free_model_reserves_zero_when_paid_cap_is_zero(self):
        # A zero paid daily cap denies paid calls but must not block a
        # known-free model: the zero-cost reservation cannot oversubscribe,
        # so it is admitted with an auditable reservation row.
        session = _session([_sums(0.0, 0.0), _insert(("res-free-zero",))])
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            permit = enforce_budget(
                {"budgets": {"daily_llm_usd": 0}},
                "thesis_autonomy",
                model="nvidia/nemotron-3-super-120b-a12b:free",
                correlation_id="cid-free-zero",
                component="thesis_autonomy",
            )
        self.assertTrue(permit.valid)
        self.assertEqual(permit.reservation_id, "res-free-zero")
        insert_params = session.execute.call_args_list[3].args[1]
        self.assertEqual(insert_params["estimate"], 0.0)
        self.assertEqual(insert_params["processor"], "thesis_autonomy")
        # The paid cap of zero was bypassed for the zero-cost reservation:
        # the sums query ran (spent 0.0, reserved 0.0) but no BudgetExceeded
        # was raised and the insert happened.
        self.assertEqual(len(session.execute.call_args_list), 4)

    def test_negative_cap_fails_closed_not_unlimited(self):
        with patch("budgets._reserve_budget_quota") as reserve:
            with self.assertRaises(BudgetUnavailable):
                enforce_budget(
                    {"budgets": {"daily_llm_usd": -5.0}}, "briefing"
                )
        reserve.assert_not_called()
        with self.assertRaises(ValueError):
            from budgets import get_budget_config

            get_budget_config({"budgets": {"daily_llm_usd": -0.01}})


class ReservationSettlementTests(unittest.TestCase):
    def test_settle_records_actual_and_releases(self):
        session = Mock()
        session.execute.return_value.rowcount = 1
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertTrue(settle_budget_reservation("res-1", 0.42, {}))
        sql, params = session.execute.call_args.args
        self.assertIn("status = 'settled'", str(sql))
        self.assertEqual(params["rid"], "res-1")
        self.assertEqual(params["actual"], 0.42)

    def test_settle_is_noop_without_reservation(self):
        with patch("budgets.get_session") as get_session:
            self.assertTrue(settle_budget_reservation(None, 0.1, {}))
            self.assertTrue(settle_budget_reservation("", 0.1, {}))
        get_session.assert_not_called()

    def test_settle_rejects_malformed_actual(self):
        with patch("budgets.get_session") as get_session:
            with self.assertRaises(ValueError):
                settle_budget_reservation("res-1", math.nan, {})
            with self.assertRaises(ValueError):
                settle_budget_reservation("res-1", -0.01, {})
        get_session.assert_not_called()

    def test_settle_db_failure_is_logged_not_fatal(self):
        with patch("budgets.get_session", side_effect=RuntimeError("db down")):
            self.assertFalse(settle_budget_reservation("res-1", 0.1, {}))

    def test_release_clears_active_reservation(self):
        session = Mock()
        session.execute.return_value.rowcount = 1
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertTrue(release_budget_reservation("res-1", {}))
        sql, params = session.execute.call_args.args
        self.assertIn("status = 'released'", str(sql))
        self.assertEqual(params["rid"], "res-1")

    def test_retain_settles_at_estimate_for_ambiguous_calls(self):
        session = Mock()
        session.execute.return_value.rowcount = 1
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertTrue(retain_budget_reservation("res-1", {}))
        sql, params = session.execute.call_args.args
        self.assertIn("status = 'settled'", str(sql))
        self.assertIn("settled_usd = estimated_usd", str(sql))
        self.assertEqual(params["rid"], "res-1")

    def test_expire_sweeps_abandoned_and_returns_count(self):
        session = Mock()
        session.execute.return_value.rowcount = 3
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertEqual(
                expire_abandoned_reservations({}, now=datetime.now(UTC)), 3
            )
        sql, params = session.execute.call_args.args
        self.assertIn("status = 'expired'", str(sql))
        self.assertIn("expires_at <= :now", str(sql))

    def test_settle_records_actual_even_after_expiry(self):
        # A provider call longer than the reservation TTL must still record
        # its real actual once it completes; the spend cannot vanish.
        session = Mock()
        session.execute.return_value.rowcount = 1
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            self.assertTrue(settle_budget_reservation("res-expired", 0.4, {}))
        sql, params = session.execute.call_args.args
        self.assertIn("status IN ('active', 'expired')", str(sql))
        self.assertEqual(params["actual"], 0.4)

    def test_unreserved_processing_log_counts_as_spend(self):
        # A processing_log row with no matching settled reservation is legacy
        # spend; it must count toward the cap on its started_at day.
        session = Mock()
        lock = Mock()
        lock.fetchone.return_value = None
        sweep = Mock()
        sweep.fetchone.return_value = None
        sums = Mock()
        sums.fetchone.return_value = SimpleNamespace(
            _mapping={"spent_usd": 1.2, "reserved_usd": 0.0}
        )
        session.execute.side_effect = [lock, sweep, sums]
        with patch("budgets.get_session") as get_session:
            get_session.return_value.__enter__.return_value = session
            with self.assertRaises(BudgetExceeded) as raised:
                _reserve_budget_quota(
                    {}, "briefing", cap=2.0, estimate_usd=1.0, ttl_seconds=600
                )
        self.assertAlmostEqual(raised.exception.today_cost, 1.2)


class PostgresReservationContractTests(unittest.TestCase):
    """Prove the cap cannot be oversubscribed and expiry releases funds.

    Real-PostgreSQL, env-gated by ``TEST_DATABASE_URL`` (see pg_support):
    skipped locally, run by CI against a disposable database provisioned
    from db/init + the full migration inventory.
    """

    CAP = 2.0
    ESTIMATE = 0.5  # exactly four concurrent admissions fit under the cap
    WORKERS = 8

    @classmethod
    def setUpClass(cls):
        url = require_postgres()
        cls.config = {
            **parse_config(url),
            "budgets": {
                "daily_llm_usd": cls.CAP,
                "warn_at_pct": 80,
                "reservation_estimate_usd": cls.ESTIMATE,
                "reservation_ttl_seconds": 3600,
            },
        }
        provision(cls.config)
        cls.engine = get_engine(cls.config)

    def setUp(self):
        truncate(self.config, ("budget_reservations", "processing_log"))

    def _run_concurrent(self, worker_count):
        barrier = threading.Barrier(worker_count)
        outcomes = [None] * worker_count

        def worker(index: int) -> None:
            cid = str(uuid4())
            try:
                barrier.wait(timeout=15)
                permit = enforce_budget(
                    self.config,
                    "briefing",
                    correlation_id=cid,
                    run_kind="processor",
                    component="briefing",
                )
                outcomes[index] = ("ok", permit, cid)
            except Exception as exc:  # noqa: BLE001 - collected per worker
                outcomes[index] = ("blocked", exc, cid)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(worker_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return outcomes

    def test_concurrent_admission_cannot_oversubscribe_cap(self):
        outcomes = self._run_concurrent(self.WORKERS)
        admitted = [o for o in outcomes if o and o[0] == "ok"]
        blocked = [o for o in outcomes if o and o[0] == "blocked"]

        self.assertEqual(len(admitted), 4)
        self.assertEqual(len(blocked), 4)
        for _, exc, _cid in blocked:
            self.assertIsInstance(exc, BudgetExceeded)
            self.assertEqual(exc.cap, self.CAP)

        with self.engine.connect() as conn:
            active = conn.exec_driver_sql(
                "SELECT COUNT(*), COALESCE(SUM(estimated_usd), 0), "
                "COUNT(DISTINCT correlation_id), MIN(component), MIN(run_kind) "
                "FROM budget_reservations WHERE status = 'active'"
            ).fetchone()
        self.assertEqual(active[0], 4)
        self.assertAlmostEqual(float(active[1]), 2.0)
        # Provenance: every reservation carries its correlation/component/kind.
        self.assertEqual(active[2], 4)
        self.assertEqual(active[3], "briefing")
        self.assertEqual(active[4], "processor")

        # Settling anchors the actual to its reservation day: the cap stays
        # exhausted until UTC rollover (no reflection-based release).
        for _, permit, _cid in admitted:
            settle_budget_reservation(permit.reservation_id, 0.4, self.config)
        outcomes = self._run_concurrent(2)
        self.assertEqual(
            len([o for o in outcomes if o and o[0] == "ok"]), 0
        )

        # The durable processing_log rows for those calls are excluded from
        # spend by the reservation dedupe: no double count, and the committed
        # load stays exactly the settled actuals (1.6), not 3.2.
        with self.engine.begin() as conn:
            for _status, _permit, cid in admitted:
                conn.exec_driver_sql(
                    "INSERT INTO processing_log "
                    "(started_at, completed_at, processor, status, cost_usd, correlation_id) "
                    "VALUES (NOW() - INTERVAL '1 second', NOW(), 'briefing', 'success', 0.4, %s)",
                    (cid,),
                )
        outcomes = self._run_concurrent(2)
        self.assertEqual(
            len([o for o in outcomes if o and o[0] == "ok"]), 0
        )
        blocked_exc = next(o[1] for o in outcomes if o and o[0] == "blocked")
        self.assertAlmostEqual(blocked_exc.today_cost, 1.6)

    def test_cross_midnight_paid_call_never_disappears(self):
        # A run started before midnight pays after midnight: its reservation
        # is on the new day, its processing_log row on the old day. The new
        # day must still count the settled actual (not vanish, not double).
        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        utc_day = day_start.date().isoformat()
        reserved_at = (day_start + timedelta(minutes=1)).isoformat()
        settled_at = (day_start + timedelta(minutes=2)).isoformat()
        expires_at = (day_start + timedelta(hours=2)).isoformat()
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO budget_reservations "
                "(budget_day, correlation_id, run_kind, component, processor, "
                " estimated_usd, settled_usd, status, reserved_at, expires_at, "
                " settled_at) "
                "VALUES (%s, '00000000-0000-0000-0000-0000000000c1', "
                "'processor', 'briefing', 'briefing', 1.6, 1.6, 'settled', "
                "%s, %s, %s)",
                (utc_day, reserved_at, expires_at, settled_at),
            )
            conn.exec_driver_sql(
                "INSERT INTO processing_log "
                "(started_at, completed_at, processor, status, cost_usd, correlation_id) "
                "VALUES (%s, %s, "
                "'briefing', 'success', 1.6, '00000000-0000-0000-0000-0000000000c1')",
                (
                    (day_start - timedelta(hours=25)).isoformat(),
                    settled_at,
                ),
            )
        # The settled actual (budget_day = today) blocks a new admission even
        # though the correlated log row started yesterday: the spend never
        # disappears at midnight.
        with self.assertRaises(BudgetExceeded) as raised:
            enforce_budget(
                self.config, "briefing", correlation_id="cid-midnight",
                run_kind="processor", component="briefing",
            )
        self.assertAlmostEqual(raised.exception.today_cost, 1.6)
        # And no double count: a second blocked worker sees the same 1.6.
        with self.assertRaises(BudgetExceeded) as raised:
            enforce_budget(
                self.config, "briefing", correlation_id="cid-midnight-2",
                run_kind="processor", component="briefing",
            )
        self.assertAlmostEqual(raised.exception.today_cost, 1.6)

    def test_migration_reruns_idempotently(self):
        migration = (
            REPOSITORY_ROOT / "db" / "migrations" / "045_budget_reservations.sql"
        ).read_text()
        with self.engine.begin() as conn:
            conn.exec_driver_sql(migration)
            conn.exec_driver_sql(migration)
        # The table remains usable after the second application.
        truncate(self.config, ("budget_reservations", "processing_log"))

    def test_lifecycle_invariants_reject_invalid_rows(self):
        from sqlalchemy.exc import IntegrityError

        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        reserved_at = (day_start + timedelta(minutes=1)).isoformat()
        base = {
            "budget_day": day_start.date().isoformat(),
            "run_kind": "processor",
            "component": "briefing",
            "processor": "briefing",
            "estimated_usd": 0.5,
            "status": "active",
            "reserved_at": reserved_at,
            "expires_at": (day_start + timedelta(hours=1)).isoformat(),
        }
        cases = {
            "blank_processor": {"processor": "  "},
            "blank_component": {"component": ""},
            "active_with_settled": {
                "status": "active",
                "settled_usd": 0.1,
                "settled_at": reserved_at,
            },
            "settled_incomplete": {"status": "settled"},
            "released_incomplete": {"status": "released"},
            "expired_with_settled": {
                "status": "expired",
                "settled_usd": 0.1,
                "settled_at": reserved_at,
            },
            "settle_before_reserve": {
                "status": "settled",
                "settled_usd": 0.1,
                "settled_at": (day_start - timedelta(minutes=1)).isoformat(),
            },
            "day_mismatch": {"budget_day": (day_start - timedelta(days=1)).date().isoformat()},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                row = {**base, **overrides}
                columns = ", ".join(row)
                placeholders = ", ".join("%s" for _ in row)
                with self.engine.begin() as conn, self.assertRaises(IntegrityError):
                    conn.exec_driver_sql(
                        f"INSERT INTO budget_reservations ({columns}) "
                        f"VALUES ({placeholders})",
                        tuple(row.values()),
                    )

    @staticmethod
    def _insert_settled_reservation(conn, correlation_id, settled_usd, day_start):
        conn.exec_driver_sql(
            "INSERT INTO budget_reservations "
            "(budget_day, correlation_id, run_kind, component, processor, "
            " estimated_usd, settled_usd, status, reserved_at, expires_at, "
            " settled_at) "
            "VALUES (%s, %s, 'processor', 'briefing', 'briefing', %s, %s, "
            "'settled', %s, %s, %s)",
            (
                day_start.date().isoformat(),
                correlation_id,
                settled_usd,
                settled_usd,
                (day_start + timedelta(minutes=1)).isoformat(),
                (day_start + timedelta(hours=2)).isoformat(),
                (day_start + timedelta(minutes=2)).isoformat(),
            ),
        )

    def _config_with(self, cap, estimate):
        return {
            **self.config,
            "budgets": {
                **self.config["budgets"],
                "daily_llm_usd": cap,
                "reservation_estimate_usd": estimate,
            },
        }

    def test_mixed_legacy_and_reserved_calls_reconcile_per_run(self):
        # One processing_log row mixes an unreserved legacy call (0.3) with
        # two reserved calls (0.1 + 0.1). Per-pair reconciliation keeps the
        # unreserved remainder: committed must be 0.5, not 0.2 (lost) or 0.7.
        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO processing_log "
                "(started_at, completed_at, processor, status, cost_usd, correlation_id) "
                "VALUES (%s, %s, 'briefing', 'success', 0.5, '00000000-0000-0000-0000-0000000000aa')",
                (
                    (day_start + timedelta(minutes=3)).isoformat(),
                    (day_start + timedelta(minutes=4)).isoformat(),
                ),
            )
            for _index in range(2):
                self._insert_settled_reservation(
                    conn,
                    "00000000-0000-0000-0000-0000000000aa",
                    0.1,
                    day_start,
                )
        config = self._config_with(cap=0.8, estimate=0.4)
        with self.assertRaises(BudgetExceeded) as raised:
            enforce_budget(
                config, "briefing", correlation_id="cid-mixed",
                run_kind="processor", component="briefing",
            )
        self.assertAlmostEqual(raised.exception.today_cost, 0.5)

    def test_log_cost_below_settled_actual_never_double_counts(self):
        # The processing_log row (0.05) records less than the settled actual
        # (0.1). The settled actual is authoritative: committed is 0.1, never
        # 0.15 (log + settlement) and never 0.05 (lost remainder).
        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO processing_log "
                "(started_at, completed_at, processor, status, cost_usd, correlation_id) "
                "VALUES (%s, %s, 'briefing', 'success', 0.05, '00000000-0000-0000-0000-0000000000bb')",
                (
                    (day_start + timedelta(minutes=3)).isoformat(),
                    (day_start + timedelta(minutes=4)).isoformat(),
                ),
            )
            self._insert_settled_reservation(
                conn,
                "00000000-0000-0000-0000-0000000000bb",
                0.1,
                day_start,
            )
        config = self._config_with(cap=0.4, estimate=0.4)
        with self.assertRaises(BudgetExceeded) as raised:
            enforce_budget(
                config, "briefing", correlation_id="cid-small",
                run_kind="processor", component="briefing",
            )
        self.assertAlmostEqual(raised.exception.today_cost, 0.1)

    def test_cross_midnight_reservation_owns_the_paid_call(self):
        # A run started today (log in today's window) whose paid call was
        # admitted after midnight (settled reservation on tomorrow) must not
        # count as full legacy spend on today: the reservation day owns the
        # call, so today's residual is the log total minus the settled actual.
        day_start = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        tomorrow = day_start + timedelta(days=1)
        with self.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO processing_log "
                "(started_at, completed_at, processor, status, cost_usd, correlation_id) "
                "VALUES (%s, %s, 'briefing', 'success', 0.5, '00000000-0000-0000-0000-0000000000cc')",
                (
                    (day_start + timedelta(minutes=3)).isoformat(),
                    (day_start + timedelta(minutes=4)).isoformat(),
                ),
            )
            conn.exec_driver_sql(
                "INSERT INTO budget_reservations "
                "(budget_day, correlation_id, run_kind, component, processor, "
                " estimated_usd, settled_usd, status, reserved_at, expires_at, "
                " settled_at) "
                "VALUES (%s, '00000000-0000-0000-0000-0000000000cc', "
                "'processor', 'briefing', 'briefing', 0.2, 0.2, 'settled', "
                "%s, %s, %s)",
                (
                    tomorrow.date().isoformat(),
                    (tomorrow + timedelta(minutes=1)).isoformat(),
                    (tomorrow + timedelta(hours=2)).isoformat(),
                    (tomorrow + timedelta(minutes=2)).isoformat(),
                ),
            )
        config = self._config_with(cap=0.4, estimate=0.4)
        with self.assertRaises(BudgetExceeded) as raised:
            enforce_budget(
                config, "briefing", correlation_id="cid-midnight-owns",
                run_kind="processor", component="briefing",
            )
        # 0.3 = 0.5 log minus 0.2 reserved actual; the reserved 0.2 belongs to
        # tomorrow and is NOT counted on today (would be 0.5 with the old join).
        self.assertAlmostEqual(raised.exception.today_cost, 0.3)

    def test_expiry_releases_reserved_funds(self):
        # One reservation (1.5) fills the cap, so a second call is blocked
        # until the first expires and its estimate is released. The TTL gate
        # normally forbids sub-deadline TTLs, so the expiry mechanics are
        # exercised with a directly injected short policy.
        short_ttl = self.config
        with patch("budgets._reservation_policy", return_value=(1.5, 0.3)):
            first = enforce_budget(
                short_ttl, "briefing", correlation_id=str(uuid4()),
                run_kind="processor", component="briefing",
            )
            self.assertTrue(first.valid)
            with self.assertRaises(BudgetExceeded):
                enforce_budget(
                    short_ttl, "briefing", correlation_id=str(uuid4()),
                    run_kind="processor", component="briefing",
                )
            time.sleep(0.6)
            expired_count = expire_abandoned_reservations(short_ttl)
            self.assertGreaterEqual(expired_count, 1)
            with self.engine.connect() as conn:
                row = conn.exec_driver_sql(
                    "SELECT status FROM budget_reservations WHERE id = %s",
                    (first.reservation_id,),
                ).fetchone()
            self.assertEqual(row[0], "expired")
            # Funds are released: the same worker slot admits again.
            second = enforce_budget(
                short_ttl, "briefing", correlation_id=str(uuid4()),
                run_kind="processor", component="briefing",
            )
            self.assertTrue(second.valid)
            settle_budget_reservation(second.reservation_id, 0.1, short_ttl)


if __name__ == "__main__":
    unittest.main()
