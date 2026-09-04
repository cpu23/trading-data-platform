"""Tests for bounded thesis proposal review queue JSON and view routes.

Covers:
- GET /api/research/theses/proposals and GET /api/research/proposals (bounded list, filtering, pagination)
- GET /api/research/theses/proposals/{id} and GET /api/research/proposals/{id} (detail, 404, 422)
- POST /api/research/theses/proposals/{id}/approve (auth, CSRF, reviewer identity, bounds, 409 transition error)
- POST /api/research/theses/proposals/{id}/reject (auth, CSRF, reviewer identity, bounds, 409 transition error)
- POST /api/research/theses/proposals/{id}/revision (auth, CSRF, instructions validation, durable thesis_autonomy_run enqueue, 202)
- Read-only published thesis views vs proposal review views
"""

import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

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
        "CONFIG_DIR": str(Path(__file__).resolve().parents[2] / "config"),
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import mint_csrf_token  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MOCK_CONFIG = {
    "logging": {"level": "INFO"},
    "database": {
        "host": "localhost",
        "port": 5432,
        "name": "test",
        "user": "test",
        "password": "test",
    },
    "dashboard": {"stale_thresholds": {}},
    "collectors": {},
    "processors": {},
    "budgets": {"daily_llm_usd": 2.00, "warn_at_pct": 80},
}

with patch("config.load_config", return_value=MOCK_CONFIG):
    from main import app  # noqa: E402

client = TestClient(app)
client.__enter__()

CSRF_TOKEN = mint_csrf_token()
AUTH = {
    "Authorization": "Basic dGVzdDp0ZXN0",  # test:test
    "Origin": "http://testserver",
    "X-CSRF-Token": CSRF_TOKEN,
}
client.cookies.set("csrf-token", CSRF_TOKEN)

NOW = datetime(2026, 8, 15, 10, 30, tzinfo=UTC)
PROPOSAL_ID = UUID("11111111-1111-4111-8111-111111111111")
THEME_ID = UUID("22222222-2222-4222-8222-222222222222")


def tearDownModule():
    client.__exit__(None, None, None)


def _sample_proposal(
    *,
    proposal_id: UUID = PROPOSAL_ID,
    status: str = "pending_review",
    company: str = "Nvidia Corp",
    symbol: str = "NVDA",
    reviewer_id: str | None = None,
    review_note: str | None = None,
    revision_instructions: str | None = None,
) -> dict:
    return {
        "id": proposal_id,
        "proposal_key": "proposal:test-cycle:nvda",
        "canonical_key": "canonical:test:nvda",
        "theme_id": THEME_ID,
        "company": company,
        "symbol": symbol,
        "subject": "Data center accelerator revenue growth outpaces consensus",
        "direction": "bullish",
        "horizon": "12m",
        "mechanism": "Custom silicon margins expand alongside hyperscaler capex",
        "status": status,
        "payload": {
            "claim": "NVDA data center revenue sustained acceleration",
            "opportunity_score": 0.85,
        },
        "evidence": [
            {"claim_id": "claim:001", "relationship": "supports", "strength": 0.9}
        ],
        "scenarios": [
            {"name": "bull", "probability": 0.5, "expected_return": 0.35},
            {"name": "base", "probability": 0.35, "expected_return": 0.15},
            {"name": "bear", "probability": 0.15, "expected_return": -0.20},
        ],
        "scoring": {
            "opportunity_score": 0.85,
            "confidence_score": 0.82,
            "expected_value": 0.22,
        },
        "challenge": {
            "runner_failed": False,
            "contradiction_count": 0,
            "findings": [],
        },
        "diff": {
            "action": "create",
            "changes": {"subject": "Data center accelerator revenue growth"},
        },
        "matching_thesis_id": None,
        "materialized_thesis_id": None,
        "reviewer_id": reviewer_id,
        "review_note": review_note,
        "reviewed_at": NOW if status != "pending_review" else None,
        "parent_proposal_id": None,
        "revision_instructions": revision_instructions,
        "accepted_reference": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }


class ResearchReviewQueueListDetailTests(unittest.TestCase):
    """GET /api/research/theses/proposals and GET /api/research/theses/proposals/{id}."""

    def test_list_proposals_returns_bounded_json(self):
        sample = _sample_proposal()
        with patch(
            "routes.json.research._thesis_fusion.list_thesis_proposals",
            return_value=[sample],
        ) as mock_list:
            response = client.get(
                "/api/research/theses/proposals?status=pending_review&limit=25&offset=0",
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("proposals", data)
            self.assertEqual(len(data["proposals"]), 1)
            self.assertEqual(data["proposals"][0]["id"], str(PROPOSAL_ID))
            self.assertEqual(data["proposals"][0]["status"], "pending_review")
            self.assertEqual(data["limit"], 25)
            self.assertEqual(data["offset"], 0)
            mock_list.assert_called_once()

    def test_list_proposals_alias_route(self):
        sample = _sample_proposal()
        with patch(
            "routes.json.research._thesis_fusion.list_thesis_proposals",
            return_value=[sample],
        ):
            response = client.get("/api/research/proposals", headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["proposals"]), 1)

    def test_list_proposals_validates_theme_uuid(self):
        response = client.get(
            "/api/research/theses/proposals?theme_id=not-a-uuid",
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 422)

    def test_list_proposals_bounds_limit_and_offset(self):
        response = client.get(
            "/api/research/theses/proposals?limit=999",
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 422)
        response_neg = client.get(
            "/api/research/theses/proposals?offset=-5",
            headers=AUTH,
        )
        self.assertEqual(response_neg.status_code, 422)

    def test_proposal_detail_returns_existing_proposal(self):
        sample = _sample_proposal()
        with patch(
            "routes.json.research._thesis_fusion.get_thesis_proposal",
            return_value=sample,
        ) as mock_get:
            response = client.get(
                f"/api/research/theses/proposals/{PROPOSAL_ID}",
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("proposal", data)
            self.assertEqual(data["proposal"]["id"], str(PROPOSAL_ID))
            self.assertEqual(data["proposal"]["company"], "Nvidia Corp")
            self.assertIn("diff", data["proposal"])
            self.assertIn("scoring", data["proposal"])
            self.assertIn("challenge", data["proposal"])
            mock_get.assert_called_once()

    def test_proposal_detail_alias_route(self):
        sample = _sample_proposal()
        with patch(
            "routes.json.research._thesis_fusion.get_thesis_proposal",
            return_value=sample,
        ):
            response = client.get(
                f"/api/research/proposals/{PROPOSAL_ID}",
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["proposal"]["id"], str(PROPOSAL_ID))

    def test_proposal_detail_404_when_missing(self):
        with patch(
            "routes.json.research._thesis_fusion.get_thesis_proposal",
            return_value=None,
        ):
            response = client.get(
                f"/api/research/theses/proposals/{PROPOSAL_ID}",
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 404)

    def test_proposal_detail_422_on_invalid_uuid(self):
        response = client.get(
            "/api/research/theses/proposals/invalid-uuid",
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 422)

    def test_theses_proposals_route_precedence_over_thesis_detail(self):
        sample = _sample_proposal()
        thesis_payload = {"thesis": {"id": str(PROPOSAL_ID), "name": "Canonical"}}
        with (
            patch(
                "routes.json.research._thesis_fusion.list_thesis_proposals",
                return_value=[sample],
            ) as mock_proposals,
            patch(
                "routes.json.research._thesis_fusion.load_thesis_detail",
                return_value=thesis_payload,
            ) as mock_thesis,
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response_proposals = client.get(
                "/api/research/theses/proposals?status=pending_review&limit=25&offset=0",
                headers=AUTH,
            )
            self.assertEqual(response_proposals.status_code, 200)
            self.assertEqual(len(response_proposals.json()["proposals"]), 1)
            mock_proposals.assert_called_once()
            mock_thesis.assert_not_called()

            response_thesis = client.get(
                f"/api/research/theses/{PROPOSAL_ID}",
                headers=AUTH,
            )
            self.assertEqual(response_thesis.status_code, 200)
            self.assertEqual(
                response_thesis.json()["thesis"]["thesis"]["id"], str(PROPOSAL_ID)
            )
            mock_thesis.assert_called_once()


class ResearchReviewQueueApproveTests(unittest.TestCase):
    """POST /api/research/theses/proposals/{id}/approve."""

    def test_approve_success_transitions_and_materializes(self):
        approved = _sample_proposal(
            status="approved",
            reviewer_id="test",
            review_note="Solid data center thesis",
        )
        approved["materialized_thesis_id"] = str(uuid4())
        with (
            patch(
                "routes.json.research._thesis_fusion.approve_thesis_proposal",
                return_value=approved,
            ) as mock_approve,
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/approve",
                json={"review_note": "Solid data center thesis"},
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "approved")
            self.assertEqual(data["proposal"]["status"], "approved")
            self.assertEqual(data["proposal"]["reviewer_id"], "test")
            self.assertIsNotNone(data["proposal"]["materialized_thesis_id"])
            mock_approve.assert_called_once()
            call_kwargs = mock_approve.call_args.kwargs
            self.assertEqual(call_kwargs["reviewer_id"], "test")
            self.assertEqual(call_kwargs["review_note"], "Solid data center thesis")

    def test_approve_validates_proposal_uuid(self):
        response = client.post(
            "/api/research/theses/proposals/invalid-uuid/approve",
            json={},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 422)

    def test_approve_rejects_overly_long_review_note(self):
        response = client.post(
            f"/api/research/theses/proposals/{PROPOSAL_ID}/approve",
            json={"review_note": "x" * 4001},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 422)

    def test_approve_maps_not_found_to_404(self):
        with (
            patch(
                "routes.json.research._thesis_fusion.approve_thesis_proposal",
                side_effect=ValueError(f"proposal {PROPOSAL_ID} not found"),
            ),
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/approve",
                json={},
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 404)

    def test_approve_maps_invalid_transition_to_409(self):
        with (
            patch(
                "routes.json.research._thesis_fusion.approve_thesis_proposal",
                side_effect=ValueError(
                    "cannot approve proposal with status 'approved'; only pending_review can transition"
                ),
            ),
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/approve",
                json={},
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 409)


class ResearchReviewQueueRejectTests(unittest.TestCase):
    """POST /api/research/theses/proposals/{id}/reject."""

    def test_reject_success_transitions_proposal(self):
        rejected = _sample_proposal(
            status="rejected",
            reviewer_id="test",
            review_note="Insufficient differentiation",
        )
        with (
            patch(
                "routes.json.research._thesis_fusion.reject_thesis_proposal",
                return_value=rejected,
            ) as mock_reject,
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/reject",
                json={"review_note": "Insufficient differentiation"},
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "rejected")
            self.assertEqual(data["proposal"]["status"], "rejected")
            self.assertIsNone(data["proposal"]["materialized_thesis_id"])
            mock_reject.assert_called_once()
            call_kwargs = mock_reject.call_args.kwargs
            self.assertEqual(call_kwargs["reviewer_id"], "test")
            self.assertEqual(call_kwargs["review_note"], "Insufficient differentiation")

    def test_reject_validates_uuid_and_bounded_note(self):
        resp_uuid = client.post(
            "/api/research/theses/proposals/bad-uuid/reject",
            json={},
            headers=AUTH,
        )
        self.assertEqual(resp_uuid.status_code, 422)

        resp_long = client.post(
            f"/api/research/theses/proposals/{PROPOSAL_ID}/reject",
            json={"review_note": "x" * 4001},
            headers=AUTH,
        )
        self.assertEqual(resp_long.status_code, 422)

    def test_reject_maps_invalid_transition_to_409(self):
        with (
            patch(
                "routes.json.research._thesis_fusion.reject_thesis_proposal",
                side_effect=ValueError(
                    "cannot reject proposal with status 'rejected'; only pending_review can transition"
                ),
            ),
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/reject",
                json={},
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 409)


class ResearchReviewQueueRevisionTests(unittest.TestCase):
    """POST /api/research/theses/proposals/{id}/revision."""

    def test_revision_requires_instructions(self):
        resp_empty = client.post(
            f"/api/research/theses/proposals/{PROPOSAL_ID}/revision",
            json={"revision_instructions": "   "},
            headers=AUTH,
        )
        self.assertEqual(resp_empty.status_code, 422)

        resp_missing = client.post(
            f"/api/research/theses/proposals/{PROPOSAL_ID}/revision",
            json={"review_note": "Note only"},
            headers=AUTH,
        )
        self.assertEqual(resp_missing.status_code, 422)

    def test_revision_enforces_bounded_instructions(self):
        resp_long = client.post(
            f"/api/research/theses/proposals/{PROPOSAL_ID}/revision",
            json={"revision_instructions": "a" * 4001},
            headers=AUTH,
        )
        self.assertEqual(resp_long.status_code, 422)

    def test_revision_success_enqueues_thesis_autonomy_run_and_returns_202(self):
        revised = _sample_proposal(
            status="revision_requested",
            reviewer_id="test",
            revision_instructions="Incorporate hyperscaler custom silicon counter-evidence",
            review_note="Need updated scenario probabilities",
        )
        enqueue_payload = {
            "parent_proposal_id": str(PROPOSAL_ID),
            "proposal_key": "proposal:test-cycle:nvda",
            "canonical_key": "canonical:test:nvda",
            "theme_id": str(THEME_ID),
            "company": "Nvidia Corp",
            "symbol": "NVDA",
            "subject": "Data center accelerator revenue growth outpaces consensus",
            "direction": "bullish",
            "horizon": "12m",
            "mechanism": "Custom silicon margins expand alongside hyperscaler capex",
            "revision_instructions": "Incorporate hyperscaler custom silicon counter-evidence",
            "reviewer_id": "test",
            "candidate_payload": {
                "claim": "NVDA data center revenue sustained acceleration"
            },
        }
        mock_job = MagicMock(id=uuid4(), correlation_id=uuid4())
        mock_enqueue_res = MagicMock(job=mock_job, inserted=True, suppressed=False)

        with (
            patch(
                "routes.json.research._thesis_fusion.request_thesis_proposal_revision",
                return_value={
                    "proposal": revised,
                    "enqueue_payload": enqueue_payload,
                },
            ) as mock_request_rev,
            patch("jobs.enqueue_job", return_value=mock_enqueue_res) as mock_enqueue,
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/revision",
                json={
                    "revision_instructions": "Incorporate hyperscaler custom silicon counter-evidence",
                    "review_note": "Need updated scenario probabilities",
                },
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 202)
            data = response.json()
            self.assertEqual(data["status"], "revision_requested")
            self.assertEqual(data["proposal"]["status"], "revision_requested")
            self.assertEqual(data["job_id"], str(mock_job.id))
            self.assertTrue(data["correlation_id"])

            mock_request_rev.assert_called_once()
            call_kwargs = mock_request_rev.call_args.kwargs
            self.assertEqual(call_kwargs["reviewer_id"], "test")
            self.assertEqual(
                call_kwargs["revision_instructions"],
                "Incorporate hyperscaler custom silicon counter-evidence",
            )
            self.assertEqual(
                call_kwargs["review_note"], "Need updated scenario probabilities"
            )

            mock_enqueue.assert_called_once()
            enq_kwargs = mock_enqueue.call_args.kwargs
            self.assertEqual(enq_kwargs["job_type"], "thesis_autonomy_run")
            self.assertEqual(
                enq_kwargs["payload"]["parent_proposal_id"], str(PROPOSAL_ID)
            )
            self.assertEqual(
                enq_kwargs["payload"]["revision_instructions"],
                "Incorporate hyperscaler custom silicon counter-evidence",
            )

    def test_revision_maps_invalid_transition_to_409(self):
        with (
            patch(
                "routes.json.research._thesis_fusion.request_thesis_proposal_revision",
                side_effect=ValueError(
                    "cannot request revision for proposal with status 'approved'; only pending_review can transition"
                ),
            ),
            patch("routes.json.research.get_session") as mock_session,
        ):
            mock_session.return_value.__enter__.return_value = MagicMock()
            response = client.post(
                f"/api/research/theses/proposals/{PROPOSAL_ID}/revision",
                json={"revision_instructions": "Update assumptions"},
                headers=AUTH,
            )
            self.assertEqual(response.status_code, 409)


class ResearchReviewQueueViewTests(unittest.TestCase):
    """View rendering tests for proposal review queue and read-only published theses."""

    def test_theses_page_contains_review_queue_section(self):
        response = client.get("/research/theses", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("thesis-review-queue-section", response.text)
        self.assertIn("Thesis proposal review queue", response.text)
        self.assertIn("data-thesis-proposals", response.text)

    def test_proposals_view_route(self):
        response = client.get("/research/proposals", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertIn("thesis-review-queue-section", response.text)

    def test_proposal_detail_view_includes_review_actions(self):
        response = client.get(
            f"/research/theses/proposals/{PROPOSAL_ID}",
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("thesis-proposal-review-box", response.text)
        self.assertIn('data-proposal-action="approve"', response.text)
        self.assertIn('data-proposal-action="revision"', response.text)
        self.assertIn('data-proposal-action="reject"', response.text)
        self.assertIn("data-proposal-diff", response.text)

    def test_published_thesis_view_is_read_only_without_review_box(self):
        response = client.get(
            f"/research/theses/{PROPOSAL_ID}",
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("thesis-proposal-review-box", response.text)


if __name__ == "__main__":
    unittest.main()
