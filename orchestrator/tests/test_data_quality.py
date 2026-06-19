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
