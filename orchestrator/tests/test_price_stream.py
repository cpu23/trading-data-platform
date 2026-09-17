import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from price_stream import _extract_quote_state, db_snapshot


class PriceStreamSnapshotTests(unittest.TestCase):
    def test_extract_quote_state_running_connected(self):
        hb = {
            "status": "running",
            "detail": {"quotes": {"status": "connected", "error": None}},
        }
        status, error = _extract_quote_state(hb)
        self.assertEqual(status, "connected")
        self.assertIsNone(error)

    def test_extract_quote_state_running_simulated(self):
        hb = {
            "status": "running",
            "detail": {"quotes": {"status": "simulated", "error": None}},
        }
        status, error = _extract_quote_state(hb)
        self.assertEqual(status, "simulated")
        self.assertIsNone(error)

    def test_extract_quote_state_running_reconnecting_with_error(self):
        hb = {
            "status": "running",
            "detail": {"quotes": {"status": "reconnecting", "error": "rate limited"}},
        }
        status, error = _extract_quote_state(hb)
        self.assertEqual(status, "reconnecting")
        self.assertEqual(error, "rate limited")

    def test_extract_quote_state_stopped_worker_reports_stopped(self):
        hb = {
            "status": "stopped",
            "detail": {"quotes": {"status": "connected", "error": None}},
        }
        status, error = _extract_quote_state(hb)
        self.assertEqual(status, "stopped")

    def test_extract_quote_state_missing_or_invalid_detail(self):
        self.assertEqual(_extract_quote_state({}), ("stopped", None))
        self.assertEqual(
            _extract_quote_state({"status": "running", "detail": "not-a-dict"}),
            ("stopped", None),
        )
        self.assertEqual(
            _extract_quote_state(
                {"status": "running", "detail": {"quotes": "not-a-dict"}}
            ),
            ("stopped", None),
        )

    def test_db_snapshot_prefers_healthy_worker_over_newer_stopped(self):
        now = datetime.now(UTC)
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value = [
            {
                "symbol": "EURUSD",
                "price": 1.085,
                "observed_at": now,
                "updated_at": now,
            }
        ]
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_session
        mock_ctx.__exit__.return_value = False

        heartbeats = [
            # Newer stopped sibling.
            {
                "role": "worker",
                "instance_id": "worker-b",
                "status": "stopped",
                "last_heartbeat_at": now,
                "detail": {"quotes": {"status": "stopped"}},
            },
            # Older running replica with active quotes stream.
            {
                "role": "worker",
                "instance_id": "worker-a",
                "status": "running",
                "last_heartbeat_at": now - timedelta(seconds=2),
                "detail": {"quotes": {"status": "connected", "error": None}},
            },
        ]

        with (
            patch("price_stream.get_session", return_value=mock_ctx),
            patch("price_stream.fresh_role_heartbeats", return_value=heartbeats),
        ):
            snapshot = db_snapshot({})

        self.assertEqual(snapshot["stream"]["status"], "connected")
        self.assertEqual(snapshot["stream"]["healthy_instances"], 1)
        self.assertEqual(snapshot["stream"]["instances"], 2)
        self.assertEqual(len(snapshot["quotes"]), 1)
        self.assertEqual(snapshot["quotes"][0]["symbol"], "EURUSD")

    def test_db_snapshot_empty_heartbeats(self):
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value.execute.return_value.mappings.return_value = []
        mock_ctx.__exit__.return_value = False

        with (
            patch("price_stream.get_session", return_value=mock_ctx),
            patch("price_stream.fresh_role_heartbeats", return_value=[]),
        ):
            snapshot = db_snapshot({})

        self.assertEqual(snapshot["stream"]["status"], "stopped")
        self.assertIsNone(snapshot["stream"]["last_heartbeat"])
        self.assertIsNone(snapshot["stream"]["error"])
        self.assertEqual(snapshot["quotes"], [])
