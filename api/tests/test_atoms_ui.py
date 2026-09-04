import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

os.environ.update(
    {
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "FRED_API_KEY": "test",
        "OPENROUTER_API_KEY": "test",
        "OPENROUTER_MODEL": "test/model",
        "OANDA_API_KEY": "test",
        "DEPLOYMENT_MODE": "test",
        "SECRETS_FILE": "/nonexistent/test-secrets.env",
    }
)

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))
os.environ["CONFIG_DIR"] = str(API_ROOT.parent / "config")

AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
ATOM_ID = UUID("22222222-2222-4222-8222-222222222222")
PRIOR_ID = UUID("55555555-5555-4555-8555-555555555555")


def atom_row(status="published"):
    return {
        "id": ATOM_ID,
        "subject_type": "macro_series",
        "subject_id": "CPIAUCSL",
        "claim_type": "event_interpretation",
        "claim": "US inflation cooled relative to consensus.",
        "observation_text": "CPI printed below consensus.",
        "interpretation_text": "Disinflation continues.",
        "scenario_text": None,
        "unknowns": ["revision risk"],
        "affected_assets": ["EURUSD"],
        "time_horizon": "48h",
        "confidence": 0.7,
        "valid_from": NOW,
        "expires_at": NOW,
        "status": status,
        "model_slug": "deepseek/deepseek-v4-flash",
        "prompt_version": "event_impact_v1",
        "published_at": NOW,
        "created_at": NOW,
        "evidence": [
            {
                "evidence_type": "macro_series",
                "evidence_id": "CPIAUCSL",
                "relationship": "supports",
            }
        ],
    }


def history_row(status="superseded"):
    return {
        "id": PRIOR_ID,
        "subject_type": "macro_series",
        "subject_id": "CPIAUCSL",
        "status": status,
        "confidence": 0.6,
        "valid_from": NOW,
        "expires_at": NOW,
        "updated_at": NOW,
        "supersedes_atom_id": PRIOR_ID,
    }


def serialized_atom():
    """Shape produced by load_atom_context: ISO strings, grouped history."""
    row = atom_row()
    for key in ("valid_from", "expires_at", "published_at", "created_at"):
        row[key] = row[key].isoformat()
    row["history"] = [serialized_history()]
    return row


def serialized_history():
    row = history_row()
    for key in ("valid_from", "expires_at", "updated_at"):
        row[key] = row[key].isoformat()
    row["supersedes_atom_id"] = str(PRIOR_ID)
    return row


class AtomApiTests(unittest.TestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from main import app

        return TestClient(app)

    def test_invalid_subject_rejected_before_database_access(self):
        client = self._client()
        with patch("routes.json.atoms.query_many") as query:
            response = client.get(
                "/api/analysis/atoms?subject_type=provider_secret", headers=AUTH
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                client.get("/api/analysis/atoms?limit=0", headers=AUTH).status_code,
                422,
            )
        query.assert_not_called()

    def test_database_failure_is_generic_and_fail_soft(self):
        client = self._client()
        with patch(
            "routes.json.atoms.query_many", side_effect=RuntimeError("secret sql")
        ):
            response = client.get("/api/analysis/atoms", headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret sql", response.text)

    def test_list_returns_bounded_atoms_with_evidence(self):
        client = self._client()
        with patch("routes.json.atoms.query_many", return_value=[atom_row()]):
            response = client.get("/api/analysis/atoms", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["atoms"]), 1)
        atom = payload["atoms"][0]
        self.assertEqual(atom["subject_id"], "CPIAUCSL")
        self.assertEqual(atom["evidence"][0]["evidence_type"], "macro_series")
        self.assertNotIn("raw_payload", response.text)

    def test_detail_includes_history_and_unknown_atom_is_404(self):
        client = self._client()
        with patch(
            "routes.json.atoms.query_many",
            side_effect=[[atom_row()], [history_row()]],
        ):
            response = client.get(f"/api/analysis/atoms/{ATOM_ID}", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        history = response.json()["atom"]["history"]
        self.assertEqual(history[0]["status"], "superseded")
        self.assertEqual(history[0]["supersedes_atom_id"], str(PRIOR_ID))
        with patch("routes.json.atoms.query_many", return_value=[]):
            missing = client.get(f"/api/analysis/atoms/{ATOM_ID}", headers=AUTH)
        self.assertEqual(missing.status_code, 404)
        with patch("routes.json.atoms.query_many") as query:
            invalid = client.get("/api/analysis/atoms/not-a-uuid", headers=AUTH)
        self.assertEqual(invalid.status_code, 422)
        query.assert_not_called()


class ClaimHistoryPartialTests(unittest.TestCase):
    def _app(self):
        from fastapi import FastAPI
        from fastapi.templating import Jinja2Templates
        from routes.views.operations import router

        app = FastAPI()
        app.state.templates = Jinja2Templates(directory=API_ROOT / "templates")
        app.include_router(router)
        return app

    def test_partial_renders_claim_cards_and_superseded_history(self):
        from fastapi.testclient import TestClient

        context = {"status": "published", "atoms": [serialized_atom()]}
        with (
            patch("routes.views.operations.load_atom_context", return_value=context),
            patch("routes.views.operations.load_config", return_value={}),
        ):
            response = TestClient(self._app()).get("/partials/operations/claim-history")
        self.assertEqual(response.status_code, 200)
        self.assertIn("US inflation cooled relative to consensus.", response.text)
        self.assertIn("claim-status-published", response.text)
        self.assertIn("superseded", response.text)
        self.assertIn("macro_series · CPIAUCSL", response.text)

    def test_partial_fail_soft_and_empty_state(self):
        from fastapi.testclient import TestClient

        with (
            patch(
                "routes.views.operations.load_atom_context",
                side_effect=RuntimeError("secret sql"),
            ),
            patch("routes.views.operations.load_config", return_value={}),
        ):
            response = TestClient(self._app()).get("/partials/operations/claim-history")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unavailable", response.text)
        self.assertNotIn("secret sql", response.text)
        with (
            patch(
                "routes.views.operations.load_atom_context",
                return_value={"status": "published", "atoms": []},
            ),
            patch("routes.views.operations.load_config", return_value={}),
        ):
            empty = TestClient(self._app()).get("/partials/operations/claim-history")
        self.assertIn("No published analysis atoms yet", empty.text)


if __name__ == "__main__":
    unittest.main()
