import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.update(
    {
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "DASHBOARD_USER": "test",
        "DASHBOARD_PASSWORD": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["CONFIG_DIR"] = str(API_ROOT.parent / "config")

from auth import mint_csrf_token  # noqa: E402
from main import app  # noqa: E402

AUTH = {
    "Authorization": "Basic dGVzdDp0ZXN0",
    "Origin": "http://testserver",
    "X-CSRF-Token": mint_csrf_token(),
}
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
MARKER = NOW - timedelta(hours=2)


def event_rows():
    return [
        {
            "id": "e1",
            "source": "reuters",
            "event_type": "macro_release",
            "observed_at": NOW - timedelta(minutes=30),
            "effective_at": NOW - timedelta(minutes=40),
            "title": "US CPI cools",
        }
    ]


def cluster_rows():
    return [
        {
            "id": "c1",
            "title": "Fed path repricing",
            "last_material_change_at": NOW - timedelta(minutes=20),
        }
    ]


def atom_rows():
    return [
        {
            "claim_type": "regime",
            "claim": "Regime shifted to disinflation.",
            "status": "superseded",
            "created_at": NOW - timedelta(minutes=90),
            "updated_at": NOW - timedelta(minutes=10),
        }
    ]


def source_rows():
    return [
        {
            "source": "fred",
            "state": "ok",
            "updated_at": NOW - timedelta(minutes=5),
            "reason_code": None,
        }
    ]


def driver_rows():
    return [
        {
            "target": "USD",
            "driver_label": "Relative policy expectations",
            "direction": "supportive",
            "strength": "moderate",
            "horizon": "weeks",
            "valid_from": NOW - timedelta(minutes=4),
        }
    ]


def research_case_rows():
    return [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "title": "Grid equipment constraint",
            "lifecycle_state": "research_ready",
            "what_changed": "Independent evidence strengthened.",
            "last_changed_at": NOW - timedelta(minutes=3),
        }
    ]


class SinceLastViewTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(app)

    def _marker_env(self, directory):
        state = Path(directory)
        return (
            patch("routes.views.since_last_view.STATE_DIR", state),
            patch("routes.views.since_last_view.MARKER_FILE", state / "last_view.json"),
            state / "last_view.json",
        )

    def test_no_marker_shows_first_visit_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, marker_file, _ = self._marker_env(directory)
            with state_dir, marker_file:
                response = self._client().get(
                    "/partials/dashboard/since-last-view", headers=AUTH
                )
        self.assertEqual(response.status_code, 200)
        self.assertIn("No last-view marker yet", response.text)

    def test_marker_post_then_summary_lists_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, marker_file, _ = self._marker_env(directory)
            with (
                state_dir,
                marker_file,
                patch(
                    "routes.views.since_last_view.read_last_view_marker",
                    return_value=MARKER,
                ),
                patch(
                    "routes.views.since_last_view.query_many",
                    side_effect=[
                        event_rows(),
                        cluster_rows(),
                        atom_rows(),
                        source_rows(),
                        driver_rows(),
                        research_case_rows(),
                    ],
                ),
            ):
                response = self._client().get(
                    "/partials/dashboard/since-last-view", headers=AUTH
                )
        self.assertEqual(response.status_code, 200)
        self.assertIn("US CPI cools", response.text)
        self.assertIn("Fed path repricing", response.text)
        self.assertIn("Regime shifted to disinflation.", response.text)
        self.assertIn("Material events", response.text)
        self.assertIn("Relative policy expectations", response.text)
        self.assertIn("Grid equipment constraint", response.text)
        self.assertIn(
            "/research/cases/11111111-1111-4111-8111-111111111111",
            response.text,
        )

    def test_database_failure_is_fail_soft(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, marker_file, _ = self._marker_env(directory)
            with (
                state_dir,
                marker_file,
                patch(
                    "routes.views.since_last_view.read_last_view_marker",
                    return_value=MARKER,
                ),
                patch(
                    "routes.views.since_last_view.query_many",
                    side_effect=RuntimeError("secret sql"),
                ),
            ):
                response = self._client().get(
                    "/partials/dashboard/since-last-view", headers=AUTH
                )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing material changed", response.text)
        self.assertNotIn("secret sql", response.text)

    def test_post_marker_requires_auth_and_persists(self):
        client = self._client()
        self.assertEqual(client.post("/api/dashboard/last-view").status_code, 401)
        with tempfile.TemporaryDirectory() as directory:
            state_dir, marker_file, marker_path = self._marker_env(directory)
            with state_dir, marker_file:
                response = client.post("/api/dashboard/last-view", headers=AUTH)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(marker_path.exists())
                from routes.views.since_last_view import read_last_view_marker

                self.assertIsNotNone(read_last_view_marker())

    def test_stale_marker_is_clamped_to_max_age(self):
        from routes.views.since_last_view import read_last_view_marker

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "last_view.json").write_text(
                '{"last_view_at": "2020-01-01T00:00:00+00:00"}'
            )
            with patch(
                "routes.views.since_last_view.MARKER_FILE", state / "last_view.json"
            ):
                marker = read_last_view_marker()
        self.assertIsNotNone(marker)
        self.assertGreaterEqual(marker, datetime.now(UTC) - timedelta(days=8))


if __name__ == "__main__":
    unittest.main()
