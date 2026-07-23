import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class DataQualityTests(unittest.TestCase):
    """Tests for orchestrator/data_quality.py check functions.

    All DB access goes through `db.get_session`, which we mock with
    `unittest.mock.patch`.  The mock session's `execute()` returns a
    result object whose `.fetchone()` / `.fetchall()` provide canned data.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_session(self, fetchone=None, fetchall=None):
        """Build a mock session whose execute() returns a mock result."""
        result = Mock()
        # Always set return_value so fetchone() returns None (not a Mock)
        # when the caller passes fetchone=None.
        result.fetchone.return_value = fetchone
        if fetchall is not None:
            result.fetchall.return_value = fetchall
        session = Mock()
        session.execute.return_value = result
        return session

    # ------------------------------------------------------------------
    # check_freshness
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_check_freshness_returns_stale_when_no_recent_data(self, get_session):
        from data_quality import check_freshness

        # Simulate the most recent timestamp being 2 days old (48 hours),
        # which exceeds the max_age_hours of 30.
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        session = self._make_session(fetchone=(old_ts,))
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="fred",
            table="macro_series",
            timestamp_column="observed_at",
            max_age_hours=30,
            config={},
        )

        self.assertFalse(
            result["healthy"],
            "Should be unhealthy when latest data is older than max_age_hours",
        )
        self.assertIn("stale", result["detail"].lower())
        self.assertEqual(result["source_id"], "fred")
        self.assertEqual(result["state"], "stale")
        self.assertIsNotNone(result["latest_at"])
        self.assertGreater(result["age_hours"], 40)

    @patch("data_quality.get_session")
    def test_check_freshness_returns_healthy_with_recent_data(self, get_session):
        from data_quality import check_freshness

        # Most recent timestamp is only 1 hour old → still healthy.
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        session = self._make_session(fetchone=(recent_ts,))
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="oanda",
            table="market_data",
            timestamp_column="timestamp",
            max_age_hours=12,
            config={},
        )

        self.assertTrue(result["healthy"], "Data should be healthy")
        self.assertEqual(result["detail"], "fresh")
        self.assertEqual(result["source_id"], "oanda")
        self.assertIsNotNone(result["latest_at"])
        self.assertLess(result["age_hours"], 12)

    @patch("data_quality.get_session")
    def test_check_freshness_handles_empty_table(self, get_session):
        from data_quality import check_freshness

        # No rows at all — fetchone returns None.
        session = self._make_session(fetchone=None)
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="fred",
            table="macro_series",
            timestamp_column="observed_at",
            max_age_hours=30,
            config={},
        )

        self.assertFalse(result["healthy"])
        self.assertEqual(result["latest_at"], None)
        self.assertEqual(result["age_hours"], None)
        self.assertEqual(result["state"], "no_data")

    def test_disabled_source_is_not_reported_as_degraded(self):
        from data_quality import check_source_freshness

        result = check_source_freshness(
            source_id="eia",
            table="macro_series",
            timestamp_column="acquired_at",
            config={"collectors": {"eia": {"enabled": False}}},
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["source_id"], "eia")

    # ------------------------------------------------------------------
    # check_gaps
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_check_gaps_detects_missing_days(self, get_session):
        from data_quality import check_gaps

        # Create a 15-day window. Return dates that have gaps.
        today = datetime.now(timezone.utc).date()
        # Return only 10 dates out of the last 15 — leaving 5 gaps.
        rows = []
        for i in range(15):
            if i in (3, 7, 10, 12, 14):  # skip these days → gaps
                continue
            d = today - timedelta(days=i)
            rows.append((d.isoformat(),))
        # fetchall returns list of Row-like objects; our mock returns tuples
        # directly, but the code will unpack them.  Return raw tuples.
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_gaps(
            source_id="fred",
            table="macro_series",
            date_column="observed_at",
            expected_interval="1 day",
            config={},
            max_gap_days=3,
        )

        self.assertFalse(result["healthy"])
        self.assertGreater(len(result["gaps"]), 0)
        self.assertIn("gap", result["detail"].lower())

    @patch("data_quality.get_session")
    def test_check_gaps_healthy_when_no_gaps(self, get_session):
        from data_quality import check_gaps

        today = datetime.now(timezone.utc).date()
        # Every day present in the 15-day window (today back through 14 days ago)
        rows = [(today - timedelta(days=i)).isoformat() for i in range(15)]
        rows = [(r,) for r in rows]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_gaps(
            source_id="fred",
            table="macro_series",
            date_column="observed_at",
            expected_interval="1 day",
            config={},
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(len(result["gaps"]), 0)

    @patch("data_quality.get_session")
    def test_check_gaps_does_not_treat_weekends_as_missing(self, get_session):
        from data_quality import check_gaps

        today = datetime.now(timezone.utc).date()
        rows = [
            ((today - timedelta(days=i)).isoformat(),)
            for i in range(15)
            if (today - timedelta(days=i)).weekday() < 5
        ]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_gaps(
            source_id="fred",
            table="macro_series",
            date_column="observed_at",
            expected_interval="1 day",
            config={},
        )

        self.assertTrue(result["healthy"])

    @patch("data_quality.get_session")
    def test_macro_gap_check_respects_series_frequency(self, get_session):
        from data_quality import check_macro_series_gaps

        rows = [
            ("FRED:DAILY", "2026-06-01", "daily"),
            ("FRED:DAILY", "2026-06-03", "daily"),
            ("FRED:MONTHLY", "2026-04-01", "monthly"),
            ("FRED:MONTHLY", "2026-05-01", "monthly"),
        ]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_macro_series_gaps(
            "fred", {"collectors": {"fred": {"enabled": True}}}
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["source_id"], "fred")

    # ------------------------------------------------------------------
    # check_duplicates
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_check_duplicates_detects_dupes(self, get_session):
        from data_quality import check_duplicates

        # 100 total rows but only 95 distinct → 5 dupes.
        session = self._make_session(fetchone=(100, 95))
        get_session.return_value.__enter__.return_value = session

        result = check_duplicates(
            source_id="forex_factory",
            table="econ_events",
            unique_columns=["event_id"],
            config={},
        )

        self.assertFalse(result["healthy"])
        self.assertEqual(result["duplicate_count"], 5)

    @patch("data_quality.get_session")
    def test_check_duplicates_healthy_when_no_dupes(self, get_session):
        from data_quality import check_duplicates

        session = self._make_session(fetchone=(50, 50))
        get_session.return_value.__enter__.return_value = session

        result = check_duplicates(
            source_id="forex_factory",
            table="econ_events",
            unique_columns=["event_id"],
            config={},
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["duplicate_count"], 0)

    # ------------------------------------------------------------------
    # check_anomalies
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_check_anomalies_flags_outliers(self, get_session):
        from data_quality import check_anomalies

        # 30 values: 29 around 100, last 5 include an extreme outlier.
        base = [100.0] * 25
        # The "recent 5" (returned DESC, so ordered most-recent-first):
        # The code queries last 30 days ORDER BY ts DESC, then takes
        # most recent 5 for anomaly checking.  Let's create a set where
        # one of the recent 5 is wildly different.
        recent = [100.0, 100.0, 100.0, 500.0, 100.0]
        all_vals = recent + base
        rows = [(v,) for v in all_vals]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_anomalies(
            source_id="fred",
            table="macro_series",
            value_column="value",
            timestamp_column="observed_at",
            config={},
            z_threshold=5.0,
        )

        self.assertFalse(result["healthy"])
        self.assertGreater(len(result["anomalies"]), 0)
        # The anomaly should mention the outlier value 500.0
        self.assertTrue(
            any("500.0" in a for a in result["anomalies"]),
            "Anomaly list should identify the outlier value",
        )

    @patch("data_quality.get_session")
    def test_check_anomalies_healthy_without_outliers(self, get_session):
        from data_quality import check_anomalies

        # All values around 100 — no outliers.
        rows = [(100.0 + i * 0.1,) for i in range(30)]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_anomalies(
            source_id="fred",
            table="macro_series",
            value_column="value",
            timestamp_column="observed_at",
            config={},
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(len(result["anomalies"]), 0)

    @patch("data_quality.get_session")
    def test_check_anomalies_handles_insufficient_data(self, get_session):
        from data_quality import check_anomalies

        # Only 2 rows — not enough to calculate meaningful z-scores.
        rows = [(100.0,), (101.0,)]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        result = check_anomalies(
            source_id="fred",
            table="macro_series",
            value_column="value",
            timestamp_column="observed_at",
            config={},
        )

        # Should still complete gracefully (healthy or with a note)
        self.assertIsInstance(result, dict)
        self.assertIn("healthy", result)

    # ------------------------------------------------------------------
    # Frequency-aware freshness — business-day logic
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_friday_observation_fresh_on_monday_business_day(self, get_session):
        """Friday observation is fresh on Monday morning for daily business-day series."""
        from data_quality import check_freshness

        # Friday 2026-07-10 17:00 UTC
        friday_5pm = datetime(2026, 7, 10, 17, 0, 0, tzinfo=timezone.utc)
        session = self._make_session(fetchone=(friday_5pm.isoformat(),))
        get_session.return_value.__enter__.return_value = session

        # Monday 2026-07-13 09:00 UTC
        monday_9am = datetime(2026, 7, 13, 9, 0, 0, tzinfo=timezone.utc)

        with patch("data_quality.datetime") as mock_dt:
            mock_dt.now.return_value = monday_9am
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            mock_dt.fromisoformat = datetime.fromisoformat

            result = check_freshness(
                source_id="fred",
                table="macro_series",
                timestamp_column="observed_at",
                max_age_hours=30,
                config={
                    "data_quality": {
                        "fred": {"grace_periods": {"daily_business": 2}}
                    }
                },
                frequency="daily",
            )

        self.assertTrue(
            result["healthy"],
            f"Friday→Monday (1 business day) should be fresh; got {result}",
        )

    @patch("data_quality.get_session")
    def test_monthly_freshness_uses_monthly_threshold(self, get_session):
        """Monthly series freshness uses a monthly threshold (45 days), not generic hours."""
        from data_quality import check_freshness

        # 40 days ago — within 45-day monthly grace
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        session = self._make_session(fetchone=(old_ts,))
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="fred",
            table="macro_series",
            timestamp_column="observed_at",
            max_age_hours=30,  # generic — should be ignored when frequency provided
            config={
                "data_quality": {
                    "fred": {"grace_periods": {"monthly": 45}}
                }
            },
            frequency="monthly",
        )

        self.assertTrue(
            result["healthy"],
            f"40-day-old monthly data should be fresh with 45-day grace; got {result}",
        )

    @patch("data_quality.get_session")
    def test_monthly_freshness_stale_beyond_threshold(self, get_session):
        """50-day-old data exceeds 45-day monthly grace — should be stale."""
        from data_quality import check_freshness

        old_ts = (datetime.now(timezone.utc) - timedelta(days=50)).isoformat()
        session = self._make_session(fetchone=(old_ts,))
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="fred",
            table="macro_series",
            timestamp_column="observed_at",
            max_age_hours=30,
            config={
                "data_quality": {
                    "fred": {"grace_periods": {"monthly": 45}}
                }
            },
            frequency="monthly",
        )

        self.assertFalse(
            result["healthy"],
            f"50-day-old monthly data should be stale; got {result}",
        )
        self.assertIn("stale", result["detail"].lower())

    # ------------------------------------------------------------------
    # Frequency-aware freshness — future timestamps
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_future_timestamp_marked_future_not_negative_age(self, get_session):
        """Future event timestamps are marked 'future', not assigned negative age."""
        from data_quality import check_freshness

        future_ts = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        session = self._make_session(fetchone=(future_ts,))
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="fred",
            table="macro_series",
            timestamp_column="observed_at",
            max_age_hours=30,
            config={},
        )

        # Future should be reported but NOT as stale/old
        self.assertFalse(result["healthy"])
        self.assertIn("future", result["detail"].lower())
        # age_hours should be 0 or None for future, never negative
        if result["age_hours"] is not None:
            self.assertGreaterEqual(result["age_hours"], 0)

    @patch("data_quality.get_session")
    def test_future_scheduled_event_is_healthy_and_marked_future(self, get_session):
        """A future calendar event is expected data, not a corrupt observation."""
        from data_quality import check_freshness

        future_ts = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        session = self._make_session(fetchone=(future_ts,))
        get_session.return_value.__enter__.return_value = session

        result = check_freshness(
            source_id="forex_factory",
            table="econ_events",
            timestamp_column="scheduled_at",
            max_age_hours=14 * 24,
            config={},
            future_is_valid=True,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["freshness"], "future")
        self.assertEqual(result["age_hours"], 0.0)

    @patch("data_quality.get_session")
    def test_daily_frequency_resolves_daily_business_grace(self, get_session):
        """daily maps to daily_business rather than the generic 30-hour fallback."""
        from data_quality import check_freshness

        now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)  # Wednesday
        monday = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
        session = self._make_session(fetchone=(monday,))
        get_session.return_value.__enter__.return_value = session

        with patch("data_quality.datetime") as mock_dt:
            mock_dt.now.return_value = now
            result = check_freshness(
                source_id="fred",
                table="macro_series",
                timestamp_column="observed_at",
                max_age_hours=30,
                config={"data_quality": {"fred": {"grace_periods": {"daily_business": 2}}}},
                frequency="daily",
            )

        self.assertTrue(result["healthy"], result)

    # ------------------------------------------------------------------
    # Frequency-aware gaps — weekend exclusion
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_gap_check_excludes_weekends_daily_frequency(self, get_session):
        """Gap check excludes weekends for daily business-day series."""
        from data_quality import check_gaps

        # Simulate: all business days present, weekends missing
        # Monday 2026-07-13 through Friday 2026-07-17 present
        # But Sat/Sun (July 11-12) missing — shouldn't be flagged as gaps
        today = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        rows = []
        # Only include business days in the last 14-day window
        for i in range(15):
            d = (today - timedelta(days=i)).date()
            if d.weekday() < 5:  # Mon-Fri
                rows.append((d.isoformat(),))
        # So weekends are "missing" but shouldn't count as gaps

        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        with patch("data_quality.datetime") as mock_dt:
            mock_dt.now.return_value = today
            mock_dt.timezone = timezone
            mock_dt.timedelta = timedelta
            mock_dt.fromisoformat = datetime.fromisoformat

            result = check_gaps(
                source_id="fred",
                table="macro_series",
                date_column="observed_at",
                expected_interval="1 day",
                config={
                    "data_quality": {
                        "fred": {"grace_periods": {"daily_business": 2}}
                    }
                },
                frequency="daily",
            )

        self.assertTrue(
            result["healthy"],
            f"Weekend gaps should be excluded for daily frequency; got {result}",
        )
        self.assertEqual(len(result.get("gaps", [])), 0)

    # ------------------------------------------------------------------
    # Frequency-aware gaps — per-series filtering
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_gap_check_operates_per_series(self, get_session):
        """Gap check operates per series — SQL includes series_id filter."""
        from data_quality import check_gaps

        today = datetime.now(timezone.utc)
        rows = [(today - timedelta(days=i)).isoformat() for i in range(15)]
        rows = [(r,) for r in rows]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        check_gaps(
            source_id="fred",
            table="macro_series",
            date_column="observed_at",
            expected_interval="1 day",
            config={},
            series_id="GDP",
        )

        # Verify the session.execute was called with a query containing series_id
        call_args = session.execute.call_args
        sql_text = str(call_args[0][0]).lower() if call_args else ""
        params = call_args[0][1] if call_args and len(call_args[0]) > 1 else {}
        self.assertIn("series_id", sql_text,
                      f"SQL should filter by series_id; got: {sql_text}")
        self.assertIn("series_id", params,
                      f"SQL params should include series_id; got: {params}")

    # ------------------------------------------------------------------
    # Series-aware anomalies — no cross-series mixing
    # ------------------------------------------------------------------
    @patch("data_quality.get_session")
    def test_anomaly_check_never_mixes_series(self, get_session):
        """Anomaly check never mixes values from two series."""
        from data_quality import check_anomalies

        # 30 values — simulating a single series
        rows = [(100.0 + i * 0.1,) for i in range(30)]
        session = self._make_session(fetchall=rows)
        get_session.return_value.__enter__.return_value = session

        check_anomalies(
            source_id="fred",
            table="macro_series",
            value_column="value",
            timestamp_column="observed_at",
            config={},
            series_id="UNRATE",
        )

        # Verify SQL includes series_id filter
        call_args = session.execute.call_args
        sql_text = str(call_args[0][0]).lower() if call_args else ""
        params = call_args[0][1] if call_args and len(call_args[0]) > 1 else {}
        self.assertIn("series_id", sql_text,
                      f"SQL should filter by series_id; got: {sql_text}")
        self.assertIn("series_id", params,
                      f"SQL params should include series_id; got: {params}")
        # Verify the series_id value is correct
        self.assertEqual(params.get("series_id"), "UNRATE")

    def test_runner_executes_fred_checks_per_configured_series(self):
        """The production runner scopes every FRED statistic to one configured series."""
        from data_quality import run_quality_checks

        config = {
            "collectors": {"fred": {"series": [
                {"id": "CPIAUCSL", "frequency": "monthly"},
                {"id": "DGS10", "frequency": "daily"},
            ]}}
        }
        healthy = {"healthy": True, "detail": "ok"}
        with patch("data_quality.check_freshness", return_value=healthy) as freshness, \
             patch("data_quality.check_gaps", return_value=healthy) as gaps, \
             patch("data_quality.check_anomalies", return_value=healthy) as anomalies, \
             patch.dict("data_quality.DATA_QUALITY_CHECKS", {}, clear=True):
            results = run_quality_checks(config)

        self.assertEqual(set(results), {
            "fred_CPIAUCSL_freshness", "fred_CPIAUCSL_gaps", "fred_CPIAUCSL_anomalies",
            "fred_DGS10_freshness", "fred_DGS10_gaps", "fred_DGS10_anomalies",
        })
        self.assertEqual(
            [(c.kwargs["series_id"], c.kwargs["frequency"]) for c in freshness.call_args_list],
            [("CPIAUCSL", "monthly"), ("DGS10", "daily")],
        )
        self.assertEqual(
            [(c.kwargs["series_id"], c.kwargs["frequency"]) for c in gaps.call_args_list],
            [("CPIAUCSL", "monthly"), ("DGS10", "daily")],
        )
        self.assertEqual(
            [c.kwargs["series_id"] for c in anomalies.call_args_list],
            ["CPIAUCSL", "DGS10"],
        )
        self.assertEqual(results["fred_DGS10_freshness"]["frequency"], "daily")
        self.assertEqual(results["fred_DGS10_anomalies"]["source_id"], "fred")

    def test_runner_records_one_failed_check_and_continues(self):
        """One series/check failure degrades that key without suppressing later checks."""
        from data_quality import run_quality_checks

        config = {"collectors": {"fred": {"series": [
            {"id": "BROKEN", "frequency": "monthly"},
            {"id": "DGS10", "frequency": "daily"},
        ]}}}
        healthy = {"healthy": True, "detail": "ok"}
        with patch("data_quality.check_freshness", side_effect=[RuntimeError("db error"), healthy]), \
             patch("data_quality.check_gaps", return_value=healthy), \
             patch("data_quality.check_anomalies", return_value=healthy), \
             patch.dict("data_quality.DATA_QUALITY_CHECKS", {}, clear=True):
            results = run_quality_checks(config)

        self.assertFalse(results["fred_BROKEN_freshness"]["healthy"])
        self.assertIn("db error", results["fred_BROKEN_freshness"]["detail"])
        self.assertEqual(results["fred_BROKEN_freshness"]["error_type"], "RuntimeError")
        self.assertEqual(results["fred_BROKEN_freshness"]["source_id"], "fred")
        self.assertEqual(results["fred_BROKEN_freshness"]["series_id"], "BROKEN")
        self.assertEqual(results["fred_BROKEN_freshness"]["frequency"], "monthly")
        self.assertTrue(results["fred_DGS10_freshness"]["healthy"])
        self.assertEqual(len(results), 6)

    def test_runner_isolates_static_check_failure_and_redacts_credentials(self):
        """A static check failure is logged safely and does not suppress later checks."""
        from data_quality import run_quality_checks

        healthy = {"healthy": True, "detail": "ok"}
        failed = Mock(side_effect=RuntimeError(
            "connection postgresql://quality:hunter2@db/quality password=second-secret"
        ))
        later = Mock(return_value=healthy)
        registry = {
            "forex_factory_freshness": failed,
            "oanda_freshness": later,
        }
        with patch.dict("data_quality.DATA_QUALITY_CHECKS", registry, clear=True), \
             patch("data_quality.logger.error") as log_error:
            results = run_quality_checks({})

        failure = results["forex_factory_freshness"]
        self.assertFalse(failure["healthy"])
        self.assertEqual(failure["source_id"], "forex_factory")
        self.assertEqual(failure["error_type"], "RuntimeError")
        self.assertNotIn("hunter2", failure["detail"])
        self.assertNotIn("second-secret", failure["detail"])
        self.assertTrue(results["oanda_freshness"]["healthy"])
        later.assert_called_once_with({})
        log_error.assert_called_once()
        self.assertEqual(log_error.call_args.kwargs["check_id"], "forex_factory_freshness")
        self.assertEqual(log_error.call_args.kwargs["source_id"], "forex_factory")

    # ------------------------------------------------------------------
    # DATA_QUALITY_CHECKS registry
    # ------------------------------------------------------------------
    def test_all_checks_registered(self):
        from data_quality import DATA_QUALITY_CHECKS

        expected_keys = {
            "fred_freshness",
            "fred_gaps",
            "fred_anomalies",
            "forex_factory_freshness",
            "forex_factory_dupes",
            "cftc_freshness",
            "central_banks_freshness",
            "oecd_freshness",
            "ecb_freshness",
            "boe_freshness",
            "eia_freshness",
        }
        self.assertEqual(set(DATA_QUALITY_CHECKS.keys()), expected_keys)

        # Every entry must be a callable (no-op config provided).
        for key, fn in DATA_QUALITY_CHECKS.items():
            with self.subTest(check=key):
                self.assertTrue(callable(fn), f"{key} is not callable")

    @patch("data_quality.check_source_freshness")
    def test_official_registry_checks_accept_config_positionally(
        self, check_source_freshness
    ):
        from data_quality import DATA_QUALITY_CHECKS

        check_source_freshness.return_value = {"healthy": True}
        config = {"collectors": {"cftc": {"enabled": True}}}
        result = DATA_QUALITY_CHECKS["cftc_freshness"](config)

        self.assertTrue(result["healthy"])
        self.assertEqual(
            check_source_freshness.call_args.kwargs["config"],
            config,
        )


if __name__ == "__main__":
    unittest.main()
