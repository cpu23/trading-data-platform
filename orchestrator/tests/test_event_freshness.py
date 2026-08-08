import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events.freshness import (
    calculate_freshness_state,
    record_collection_freshness,
    record_event_observation,
    refresh_freshness_states,
)

NOW = datetime(2026, 8, 5, 7, 0, tzinfo=UTC)
LAST_RUN = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
SCHEDULE = "0 6 * * *"


def _classify(**overrides):
    values = {
        "enabled": True,
        "schedule": SCHEDULE,
        "last_attempt_at": LAST_RUN,
        "last_success_at": LAST_RUN,
        "last_observation_at": LAST_RUN,
        "last_material_change_at": LAST_RUN,
        "last_status": "success",
        "records_fetched": 1,
        "consecutive_failures": 0,
        "now": NOW,
        "grace_seconds": 300,
    }
    values.update(overrides)
    return calculate_freshness_state(**values)


def _result_with_first(value):
    result = MagicMock()
    result.mappings.return_value.first.return_value = value
    return result


def _result_with_all(values):
    result = MagicMock()
    result.mappings.return_value.all.return_value = values
    return result


class FreshnessClassifierTests(unittest.TestCase):
    def test_success_with_observation_is_current_before_next_schedule(self):
        result = _classify()
        self.assertEqual(result["state"], "current")
        self.assertEqual(
            result["expected_next_at"], datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
        )
        self.assertEqual(result["lag_seconds"], 0)

    def test_successful_zero_record_run_is_expected_idle_not_failed(self):
        result = _classify(records_fetched=0, last_observation_at=None)
        self.assertEqual(result["state"], "expected_idle")
        self.assertEqual(result["consecutive_failures"], 0)

    def test_explicit_failure_is_never_expected_idle(self):
        result = _classify(
            last_status="failed",
            records_fetched=0,
            consecutive_failures=2,
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["consecutive_failures"], 2)

    def test_rate_limit_and_cache_fallback_have_distinct_states(self):
        self.assertEqual(
            _classify(last_status="rate_limited", reason_code="http_429")["state"],
            "rate_limited",
        )
        self.assertEqual(
            _classify(records_fetched=0, cache_mode="stale_cache")["state"],
            "cached_fallback",
        )

    def test_never_run_disabled_and_outside_schedule_are_distinct(self):
        self.assertEqual(_classify(enabled=False)["state"], "disabled")
        self.assertEqual(
            _classify(
                last_attempt_at=None,
                last_success_at=None,
                last_observation_at=None,
                last_material_change_at=None,
                last_status=None,
                records_fetched=None,
            )["state"],
            "never_run",
        )
        self.assertEqual(_classify(schedule=None)["state"], "outside_schedule")

    def test_overdue_source_moves_from_delayed_to_stale_after_grace(self):
        next_due = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
        delayed = _classify(now=next_due + timedelta(seconds=120))
        stale = _classify(now=next_due + timedelta(seconds=301))
        self.assertEqual(delayed["state"], "delayed")
        self.assertEqual(delayed["lag_seconds"], 120)
        self.assertEqual(stale["state"], "stale")
        self.assertEqual(stale["lag_seconds"], 301)

    def test_invalid_schedule_fails_closed_without_leaking_expression(self):
        result = _classify(schedule="not a cron", detail={"safe": True})
        self.assertEqual(result["state"], "outside_schedule")
        self.assertEqual(result["detail"]["safe"], True)
        self.assertIn("error_type", result["detail"])
        self.assertNotIn("not a cron", str(result["detail"]))


class FreshnessPersistenceTests(unittest.TestCase):
    def test_collection_attempt_upserts_using_callers_transaction(self):
        session = MagicMock()
        session.execute.side_effect = [_result_with_first(None), MagicMock()]
        result = record_collection_freshness(
            session,
            source="fred",
            source_config={"enabled": True, "schedule": SCHEDULE},
            status="success",
            attempted_at=LAST_RUN,
            completed_at=LAST_RUN + timedelta(seconds=2),
            records_fetched=0,
            reason_code="no_change",
            now=NOW,
        )
        self.assertEqual(result["source"], "fred")
        self.assertEqual(result["state"], "expected_idle")
        self.assertEqual(session.execute.call_count, 2)
        insert_sql = str(session.execute.call_args_list[1].args[0])
        params = session.execute.call_args_list[1].args[1]
        self.assertIn("ON CONFLICT (source) DO UPDATE", insert_sql)
        self.assertEqual(params["source"], "fred")
        self.assertEqual(params["consecutive_failures"], 0)

    def test_duplicate_failed_attempt_does_not_increment_failure_twice(self):
        previous = {
            "source": "fred",
            "state": "failed",
            "last_attempt_at": LAST_RUN,
            "last_success_at": None,
            "last_observation_at": None,
            "last_material_change_at": None,
            "consecutive_failures": 3,
            "detail": {},
        }
        session = MagicMock()
        session.execute.side_effect = [_result_with_first(previous), MagicMock()]
        result = record_collection_freshness(
            session,
            source="fred",
            source_config={"enabled": True, "schedule": SCHEDULE},
            status="failed",
            attempted_at=LAST_RUN,
            completed_at=LAST_RUN + timedelta(seconds=1),
            records_fetched=0,
            now=NOW,
        )
        self.assertEqual(result["consecutive_failures"], 3)

    def test_event_observation_advances_timestamps_monotonically(self):
        previous = {
            "source": "fred",
            "state": "never_run",
            "last_observation_at": LAST_RUN,
            "last_material_change_at": LAST_RUN,
            "detail": {},
            "consecutive_failures": 0,
        }
        session = MagicMock()
        session.execute.side_effect = [_result_with_first(previous), MagicMock()]
        observed = LAST_RUN + timedelta(hours=2)
        event = SimpleNamespace(
            source="fred",
            observed_at=observed,
            metadata={"material_change": True},
        )
        result = record_event_observation(session, event)
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["last_observation_at"], observed)
        self.assertEqual(result["last_material_change_at"], observed)
        self.assertEqual(session.execute.call_count, 2)

    def test_periodic_refresh_marks_a_missed_schedule_stale(self):
        previous = {
            "source": "fred",
            "state": "current",
            "expected_next_at": datetime(2026, 8, 6, 6, 0, tzinfo=UTC),
            "last_attempt_at": LAST_RUN,
            "last_success_at": LAST_RUN,
            "last_observation_at": LAST_RUN,
            "last_material_change_at": LAST_RUN,
            "lag_seconds": 0.0,
            "reason_code": None,
            "detail": {},
            "cache_mode": None,
            "consecutive_failures": 0,
        }
        session = MagicMock()
        session.execute.side_effect = [
            _result_with_all([{"source": "fred"}]),
            _result_with_first(previous),
            MagicMock(),
        ]

        result = refresh_freshness_states(
            session,
            {"fred": {"enabled": True, "schedule": SCHEDULE}},
            now=datetime(2026, 8, 6, 7, 0, tzinfo=UTC),
            default_grace_seconds=300,
        )

        self.assertEqual(result, {"checked": 1, "changed": 1})
        self.assertEqual(session.execute.call_args_list[-1].args[1]["state"], "stale")


if __name__ == "__main__":
    unittest.main()
