"""Focused route tests for the autonomous thesis-desk research endpoints.

Covers the bounded GET contracts (opportunities, groups, group detail,
thesis detail, desk status) and the strict public run trigger under
``/api/research/theses/*``: query validation, JSON serialization of
datetime/UUID/Decimal values, 404/422/503 mapping, and the authenticated
proxy to the internal orchestrator enqueue route.
"""

import os
import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

os.environ["DASHBOARD_USER"] = "test"
os.environ["DASHBOARD_PASSWORD"] = "test"
os.environ["DEPLOYMENT_MODE"] = "test"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth import mint_csrf_token  # noqa: E402

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

from fastapi.testclient import TestClient  # noqa: E402

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
GROUP_ID = UUID("12121212-1212-4121-8121-121212121212")
THESIS_ID = UUID("22222222-2222-4222-8222-222222222222")


def tearDownModule():
    client.__exit__(None, None, None)


def _opportunity_row():
    return {
        "id": THESIS_ID,
        "theme_id": UUID("11111111-1111-4111-8111-111111111111"),
        "company": "Nvidia Corp",
        "symbol": "NVDA",
        "claim": "AI capex compounds.",
        "direction": "long",
        "mechanism": "AI capex compounds.",
        "horizon": "multi_year",
        "status": "active",
        "origin": "fusion",
        "evidence_strength": 0.8,
        "contradiction_strength": Decimal("0.1"),
        "neglect_score": 0.6,
        "catalyst_score": 0.5,
        "confidence_score": 0.65,
        "expected_value": Decimal("0.18"),
        "expected_shortfall": 0.05,
        "opportunity_score": 0.72,
        "last_evaluated_at": NOW,
        "last_evidence_at": NOW,
        "group_name": "NVDA bull vs bear",
    }


class ThesisDeskDeploymentWiringTests(unittest.TestCase):
    def test_source_checkout_imports_thesis_repository(self):
        from routes.json import research as research_routes

        self.assertIsNotNone(research_routes._thesis_fusion)


class ThesisDeskReadRoutesTests(unittest.TestCase):
    def test_desk_reads_require_authentication(self):
        for path in (
            "/api/research/theses/opportunities",
            "/api/research/theses/groups",
            f"/api/research/theses/groups/{GROUP_ID}",
            f"/api/research/theses/{THESIS_ID}",
            "/api/research/theses/status",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)

    def test_opportunities_validate_query_and_serialize_json_types(self):
        helpers = MagicMock()
        helpers.list_ranked_opportunities.return_value = [_opportunity_row()]
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            response = client.get(
                "/api/research/theses/opportunities"
                f"?limit=7&minimum_score=0.5&group_id={GROUP_ID}",
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["limit"], 7)
        self.assertEqual(payload["minimum_score"], 0.5)
        self.assertIs(payload["include_ineligible"], False)
        row = payload["opportunities"][0]
        self.assertEqual(row["id"], str(THESIS_ID))
        self.assertEqual(
            row["theme_id"], str(UUID("11111111-1111-4111-8111-111111111111"))
        )
        self.assertIsInstance(row["expected_value"], float)
        self.assertIsInstance(row["contradiction_strength"], float)
        self.assertEqual(row["last_evaluated_at"], NOW.isoformat())
        helpers.list_ranked_opportunities.assert_called_once()
        kwargs = helpers.list_ranked_opportunities.call_args.kwargs
        self.assertEqual(kwargs["limit"], 7)
        self.assertEqual(kwargs["minimum_score"], 0.5)
        self.assertEqual(kwargs["group_id"], str(GROUP_ID))
        self.assertIs(kwargs["include_ineligible"], False)

    def test_opportunities_serialize_unknown_scores_as_null_not_zero(self):
        # A never-evaluated thesis (migration 057) carries NULL metrics.
        # The API must emit JSON null, never a fabricated zero: unknown
        # and evaluated-zero must stay distinguishable on the wire.
        helpers = MagicMock()
        row = _opportunity_row()
        row["evidence_strength"] = None
        row["contradiction_strength"] = None
        row["neglect_score"] = None
        row["catalyst_score"] = None
        row["confidence_score"] = None
        row["expected_value"] = None
        row["expected_shortfall"] = None
        row["opportunity_score"] = None
        row["last_evaluated_at"] = None
        helpers.list_ranked_opportunities.return_value = [row]
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            response = client.get(
                "/api/research/theses/opportunities?include_ineligible=true",
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        serialized = payload["opportunities"][0]
        for column in (
            "evidence_strength",
            "contradiction_strength",
            "neglect_score",
            "catalyst_score",
            "confidence_score",
            "expected_value",
            "expected_shortfall",
            "opportunity_score",
            "last_evaluated_at",
        ):
            with self.subTest(column=column):
                self.assertIsNone(serialized[column], f"{column} must be JSON null")
        # Raw text proves the wire bytes carry null, not 0.
        self.assertIn('"opportunity_score":null', response.text)
        self.assertNotIn('"opportunity_score":0', response.text)
        self.assertIn('"neglect_score":null', response.text)

    def test_jsonable_passes_none_through_as_json_null(self):
        # The route serializer preserves None (-> JSON null) verbatim:
        # never a zero, never a string, so ranking consumers can
        # distinguish "unknown" from a measured 0.
        import json

        from routes.json.research import _jsonable

        self.assertIsNone(_jsonable(None))
        self.assertIsNone(_jsonable({"opportunity_score": None})["opportunity_score"])
        self.assertIsNone(_jsonable([None, 0])[0])
        self.assertEqual(
            json.loads(json.dumps(_jsonable({"score": None, "zero": 0}))),
            {"score": None, "zero": 0},
        )

    def test_opportunities_expose_ineligible_opt_in_truthfully(self):
        # The explicit bounded opt-in is passed through and reflected in
        # the response metadata; rows keep the loader's eligibility marks.
        helpers = MagicMock()
        row = _opportunity_row()
        row["eligible"] = False
        row["blockers"] = ["evidence"]
        helpers.list_ranked_opportunities.return_value = [row]
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            response = client.get(
                "/api/research/theses/opportunities?include_ineligible=true",
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIs(payload["include_ineligible"], True)
        self.assertEqual(payload["opportunities"][0]["blockers"], ["evidence"])
        self.assertIs(payload["opportunities"][0]["eligible"], False)
        helpers.list_ranked_opportunities.assert_called_once()
        self.assertIs(
            helpers.list_ranked_opportunities.call_args.kwargs["include_ineligible"],
            True,
        )

    def test_opportunities_reject_invalid_query_values(self):
        helpers = MagicMock()
        helpers.list_ranked_opportunities.return_value = []
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            for query in (
                "?limit=0",
                "?limit=101",
                "?minimum_score=-0.1",
                "?minimum_score=1.5",
                "?group_id=not-a-uuid",
                "?include_ineligible=maybe",
            ):
                with self.subTest(query=query):
                    response = client.get(
                        "/api/research/theses/opportunities" + query,
                        headers=AUTH,
                    )
                    self.assertEqual(response.status_code, 422)
        helpers.list_ranked_opportunities.assert_not_called()

    def test_groups_validate_status_and_serialize_aggregates(self):
        helpers = MagicMock()
        helpers.list_thesis_groups.return_value = [
            {
                "id": GROUP_ID,
                "name": "NVDA bull vs bear",
                "description": None,
                "status": "archived",
                "created_at": NOW,
                "updated_at": NOW,
                "active_members": 2,
                "long_count": 1,
                "short_count": 1,
                "neutral_count": 0,
                "max_opportunity": 0.72,
                "max_contradiction": Decimal("0.1"),
                "last_evaluation": NOW,
            }
        ]
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch(
                "routes.json.research.THESIS_GROUP_STATUSES",
                ("active", "archived"),
            ),
            patch("routes.json.research.get_session"),
        ):
            response = client.get(
                "/api/research/theses/groups?limit=5&status=archived",
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200)
        group = response.json()["groups"][0]
        self.assertEqual(group["id"], str(GROUP_ID))
        self.assertEqual(group["active_members"], 2)
        self.assertEqual(group["last_evaluation"], NOW.isoformat())
        helpers.list_thesis_groups.assert_called_once_with(
            unittest.mock.ANY, limit=5, status="archived"
        )

    def test_groups_reject_unknown_status_and_bad_limits(self):
        helpers = MagicMock()
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch(
                "routes.json.research.THESIS_GROUP_STATUSES",
                ("active", "archived"),
            ),
            patch("routes.json.research.get_session"),
        ):
            for query in (
                "?status=bogus",
                "?limit=0",
                "?limit=101",
            ):
                with self.subTest(query=query):
                    response = client.get(
                        "/api/research/theses/groups" + query,
                        headers=AUTH,
                    )
                    self.assertEqual(response.status_code, 422)
        helpers.list_thesis_groups.assert_not_called()

    def test_group_detail_missing_is_404_and_invalid_uuid_422(self):
        helpers = MagicMock()
        helpers.load_group_tournament.return_value = None
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            missing = client.get(
                f"/api/research/theses/groups/{GROUP_ID}", headers=AUTH
            )
        self.assertEqual(missing.status_code, 404)
        helpers.load_group_tournament.assert_called_once()
        with patch("routes.json.research._thesis_fusion", helpers):
            invalid = client.get("/api/research/theses/groups/not-a-uuid", headers=AUTH)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(helpers.load_group_tournament.call_count, 1)

    def test_thesis_detail_serializes_full_desk_state(self):
        helpers = MagicMock()
        helpers.load_thesis_detail.return_value = {
            "thesis": {
                "id": THESIS_ID,
                "claim": "AI capex compounds.",
                "opportunity_score": 0.72,
                "created_at": NOW,
            },
            "versions": [{"version": 2, "created_at": NOW}],
            "scenarios": [
                {
                    "id": UUID("34343434-3434-4343-8343-343434343434"),
                    "name": "Base",
                    "probability": 0.6,
                    "expected_return": Decimal("0.15"),
                    "is_base_case": True,
                    "version": 1,
                    "created_at": NOW,
                }
            ],
            "evidence": [{"relationship": "supports", "quality_score": Decimal("0.8")}],
            "catalysts": [],
            "risks": [],
            "forecasts": [],
            "outcomes": [],
            "opportunity_snapshots": [],
            "falsification_runs": [],
            "groups": [],
            "positions": [],
            "playbooks": [
                {
                    "id": UUID("79797979-7979-4797-8797-797979797979"),
                    "key": "nvda-capex-2027",
                    "version": 2,
                    "thesis_version": 1,
                    "catalyst": "Capex guidance update",
                    "horizon": "months",
                    "expected_at": NOW,
                    "event_types": ["macro_release"],
                    "trigger_conditions": ["Capex guide raised"],
                    "confirmation_conditions": [],
                    "invalidation_conditions": [],
                    "bull_scenario": None,
                    "base_scenario": None,
                    "bear_scenario": None,
                    "cited_evidence_refs": ["claim:capex-2026"],
                    "superseded_at": None,
                    "created_at": NOW,
                }
            ],
            "playbook_matches": [
                {
                    "id": UUID("80808080-8080-4808-8808-808080808080"),
                    "playbook_id": UUID("79797979-7979-4797-8797-797979797979"),
                    "event_id": UUID("81818181-8181-4818-8818-818181818181"),
                    "kind": "trigger",
                    "evidence_refs": ["claim:capex-2026"],
                    "assessment": {"confidence": 0.7},
                    "created_at": NOW,
                    "event_type": "macro_release",
                    "source": "fred",
                    "observed_at": NOW,
                }
            ],
        }
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            response = client.get(f"/api/research/theses/{THESIS_ID}", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        thesis = response.json()["thesis"]
        self.assertEqual(thesis["thesis"]["id"], str(THESIS_ID))
        self.assertEqual(thesis["thesis"]["created_at"], NOW.isoformat())
        self.assertEqual(thesis["scenarios"][0]["expected_return"], 0.15)
        self.assertEqual(
            thesis["scenarios"][0]["id"],
            str(UUID("34343434-3434-4343-8343-343434343434")),
        )
        self.assertEqual(thesis["evidence"][0]["quality_score"], 0.8)
        self.assertEqual(thesis["playbooks"][0]["key"], "nvda-capex-2027")
        self.assertEqual(thesis["playbooks"][0]["event_types"], ["macro_release"])
        self.assertEqual(
            thesis["playbooks"][0]["id"],
            str(UUID("79797979-7979-4797-8797-797979797979")),
        )
        self.assertEqual(
            thesis["playbook_matches"][0]["event_id"],
            str(UUID("81818181-8181-4818-8818-818181818181")),
        )
        self.assertEqual(thesis["playbook_matches"][0]["kind"], "trigger")
        self.assertEqual(thesis["playbook_matches"][0]["observed_at"], NOW.isoformat())
        helpers.load_thesis_detail.assert_called_once()

    def test_thesis_detail_missing_is_404_and_invalid_uuid_422(self):
        helpers = MagicMock()
        helpers.load_thesis_detail.return_value = None
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            missing = client.get(f"/api/research/theses/{THESIS_ID}", headers=AUTH)
        self.assertEqual(missing.status_code, 404)
        with patch("routes.json.research._thesis_fusion", helpers):
            invalid = client.get("/api/research/theses/not-a-uuid", headers=AUTH)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(helpers.load_thesis_detail.call_count, 1)

    def test_desk_status_serializes_availability_and_bounds_limit(self):
        helpers = MagicMock()
        helpers.thesis_desk_status.return_value = {
            "available": True,
            "theses": {"total": 3, "by_status": {"active": 2, "candidate": 1}},
            "groups": {"total": 1, "by_status": {"active": 1}},
            "ranked_theses": 2,
            "linked_theses": 1,
            "evidence": {
                "total": 4,
                "by_relationship": {"supports": 3, "contradicts": 1},
            },
            "forecasts": {"active": 5, "matured": 2},
            "outcomes": {"hit": 2, "miss": 1, "inconclusive": 1},
            "hit_rate": 2 / 3,
            "model_cost": {
                "attempts": 4,
                "known_cost_attempts": 3,
                "unknown_cost_attempts": 1,
                "today_usd": Decimal("0.06"),
                "latest_attempt_at": NOW,
            },
            "latest_evaluation_at": NOW,
            "latest_falsification_at": None,
            "sources": {
                "issuer_news": {
                    "collection": {
                        "status": "success",
                        "finished_at": NOW,
                        "records_written": 12,
                        "error_class": None,
                    },
                    "data": {
                        "available": True,
                        "latest_timestamp": NOW,
                        "acquired_at": NOW,
                    },
                },
                "issuer_transcripts": {
                    "collection": {
                        "status": "never_run",
                        "finished_at": None,
                        "records_written": 0,
                        "error_class": None,
                    },
                    "data": {
                        "available": False,
                        "latest_timestamp": None,
                        "acquired_at": None,
                    },
                    "transcript_states": {"available": 4, "setup_required": 1},
                },
            },
            "autonomy_jobs": [
                {
                    "id": UUID("56565656-5656-4565-8565-565656565656"),
                    "job_type": "thesis_autonomy_run",
                    "state": "queued",
                    "failed": False,
                    "created_at": NOW,
                }
            ],
        }
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            response = client.get("/api/research/theses/status?limit=5", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["theses"]["by_status"]["active"], 2)
        self.assertEqual(payload["forecasts"], {"active": 5, "matured": 2})
        self.assertEqual(payload["hit_rate"], 2 / 3)
        self.assertEqual(payload["model_cost"]["today_usd"], 0.06)
        self.assertEqual(payload["model_cost"]["known_cost_attempts"], 3)
        self.assertEqual(payload["model_cost"]["latest_attempt_at"], NOW.isoformat())
        self.assertEqual(payload["latest_evaluation_at"], NOW.isoformat())
        self.assertEqual(
            payload["sources"]["issuer_news"]["collection"]["finished_at"],
            NOW.isoformat(),
        )
        self.assertEqual(
            payload["sources"]["issuer_transcripts"]["transcript_states"],
            {"available": 4, "setup_required": 1},
        )
        self.assertEqual(
            payload["autonomy_jobs"][0]["id"],
            str(UUID("56565656-5656-4565-8565-565656565656")),
        )
        helpers.thesis_desk_status.assert_called_once_with(unittest.mock.ANY, limit=5)
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            invalid = client.get("/api/research/theses/status?limit=101", headers=AUTH)
        self.assertEqual(invalid.status_code, 422)

    def test_desk_reads_fail_soft_when_helpers_missing(self):
        with patch("routes.json.research._thesis_fusion", None):
            for path in (
                "/api/research/theses/opportunities",
                "/api/research/theses/groups",
                f"/api/research/theses/groups/{GROUP_ID}",
                f"/api/research/theses/{THESIS_ID}",
                "/api/research/theses/status",
            ):
                with self.subTest(path=path):
                    response = client.get(path, headers=AUTH)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["status"], "unavailable")

    def test_desk_reads_fail_soft_on_database_error(self):
        helpers = MagicMock()
        helpers.list_ranked_opportunities.side_effect = RuntimeError("secret sql")
        with (
            patch("routes.json.research._thesis_fusion", helpers),
            patch("routes.json.research.get_session"),
        ):
            response = client.get("/api/research/theses/opportunities", headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret sql", response.text)


class ThesisDeskRunRouteTests(unittest.TestCase):
    def test_desk_run_proxies_strict_body_with_budget(self):
        upstream = AsyncMock(
            return_value={
                "status": "queued",
                "job_id": "job-1",
                "correlation_id": "corr-1",
                "accepted_at": NOW.isoformat(),
                "inserted": True,
                "force": True,
            }
        )
        with (
            patch("routes.json.research._enforce_research_budget") as budget,
            patch("routes.json.research._research_orchestrator_post", upstream),
        ):
            response = client.post(
                "/api/research/theses/run",
                json={"force": True},
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "job-1")
        budget.assert_called_once_with(None)
        self.assertEqual(upstream.await_args.args[1], "/research/theses/run")
        self.assertEqual(upstream.await_args.args[2], {"force": True})

    def test_desk_run_rejects_unknown_fields_before_proxy(self):
        upstream = AsyncMock()
        with (
            patch("routes.json.research._enforce_research_budget"),
            patch("routes.json.research._research_orchestrator_post", upstream),
        ):
            for body in (
                {"force": False, "unbounded": True},
                {"force": "yes"},
                {"force": None},
            ):
                with self.subTest(body=body):
                    response = client.post(
                        "/api/research/theses/run",
                        json=body,
                        headers=AUTH,
                    )
                    self.assertEqual(response.status_code, 422)
        upstream.assert_not_called()

    def test_desk_run_requires_authentication(self):
        response = client.post("/api/research/theses/run", json={})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
