"""Focused API contracts for the autonomous research control plane."""

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

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
    "budgets": {"daily_llm_usd": 2.0, "warn_at_pct": 80},
    "research_control_plane": {
        "enabled": True,
        "priority_policy_version": "v1",
        "materiality_policy_version": "v1",
        "stale_question_days": 14,
    },
}

from fastapi.testclient import TestClient  # noqa: E402

client: TestClient
AUTH: dict[str, str]
_runtime_env_patcher = None
NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
QUESTION_ID = UUID("11111111-1111-4111-8111-111111111111")
WORK_ORDER_ID = UUID("22222222-2222-4222-8222-222222222222")
PLAN_ID = UUID("33333333-3333-4333-8333-333333333333")
JOB_ID = UUID("44444444-4444-4444-8444-444444444444")


def setUpModule():
    global AUTH, client, _runtime_env_patcher
    _runtime_env_patcher = patch.dict(
        os.environ,
        {
            "DASHBOARD_USER": "test",
            "DASHBOARD_PASSWORD": "test",
            "DEPLOYMENT_MODE": "test",
            "TRUSTED_HOSTS": "testserver,localhost",
            "EXTERNAL_ORIGIN": "http://testserver",
        },
        clear=False,
    )
    _runtime_env_patcher.start()
    with patch("config.load_config", return_value=MOCK_CONFIG):
        import main

    with patch.object(main, "load_config", return_value=MOCK_CONFIG):
        app = main.create_app()
    client = TestClient(app)
    client.__enter__()
    csrf_token = mint_csrf_token()
    AUTH = {
        "Authorization": "Basic dGVzdDp0ZXN0",
        "Origin": "http://testserver",
        "X-CSRF-Token": csrf_token,
    }
    client.cookies.set("csrf-token", csrf_token)


def tearDownModule():
    client.__exit__(None, None, None)
    if _runtime_env_patcher is not None:
        _runtime_env_patcher.stop()


class _Mappings:
    def __init__(self, *, rows=None, one=None, first=None):
        self._rows = rows or []
        self._one = one
        self._first = first

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        return self._one

    def first(self):
        return self._first


@contextmanager
def _session_context(session):
    yield session


class ResearchControlPlaneApiTests(unittest.TestCase):
    def test_questions_are_bounded_filterable_and_preserve_unknown_priority(self):
        row = {
            "id": QUESTION_ID,
            "fingerprint": "a" * 64,
            "origin_kind": "source_event",
            "question_type": "evidence_refresh",
            "atomic_question": "Did the filing change the thesis evidence?",
            "target_kind": "thesis",
            "target_ref": "NVDA-thesis",
            "accepted_cutoff": NOW,
            "required_evidence_shape": {"answer": "cited"},
            "acceptable_source_families": ["issuer_filing"],
            "materiality": None,
            "uncertainty": 0,
            "discrimination_power": 0.7,
            "urgency": 0.6,
            "freshness_gap": 1,
            "resolvability": 0.8,
            "estimated_cost_usd": 0.01,
            "estimated_runtime_seconds": 20,
            "expected_human_review_minutes": None,
            "priority_policy_version": "v1",
            "priority_score": None,
            "priority_blockers": ["materiality_unknown"],
            "status": "pending",
            "attempt_count": 0,
            "not_before": NOW,
            "due_at": None,
            "expires_at": None,
            "created_at": NOW,
            "updated_at": NOW,
            "resolved_at": None,
            "resolution_evidence_refs": [],
            "resolution_summary": None,
            "unresolved_reason": None,
        }
        session = Mock()
        session.execute.return_value = _Mappings(rows=[row])
        with (
            patch("routes.json.research.load_config", return_value=MOCK_CONFIG),
            patch(
                "routes.json.research.get_session",
                return_value=_session_context(session),
            ),
        ):
            response = client.get(
                "/api/research/questions?status=pending&target_kind=thesis&limit=1",
                headers=AUTH,
            )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIsNone(payload["items"][0]["priority"]["materiality"])
        self.assertEqual(payload["items"][0]["priority"]["uncertainty"], 0)
        params = session.execute.call_args.args[1]
        self.assertEqual(params["status"], "pending")
        self.assertEqual(params["target_kind"], "thesis")
        self.assertEqual(params["limit"], 1)
        sql = str(session.execute.call_args.args[0])
        self.assertIn("status = :status", sql)
        self.assertIn("target_kind = :target_kind", sql)
        self.assertNotIn(":status IS NULL", sql)
        self.assertNotIn(":target_kind IS NULL", sql)

    def test_invalid_list_filters_reject_before_database_access(self):
        invalid_urls = (
            "/api/research/questions?status=unknown",
            "/api/research/questions?question_type=generic",
            "/api/research/questions?target_kind=portfolio",
            "/api/research/work-orders?status=unknown",
            "/api/research/work-orders?skill_key=Bad%20Skill",
        )
        with patch("routes.json.research.get_session") as get_session:
            for url in invalid_urls:
                with self.subTest(url=url):
                    response = client.get(url, headers=AUTH)
                    self.assertEqual(response.status_code, 422, response.text)
        get_session.assert_not_called()

    def test_work_orders_exclude_inputs_results_and_private_payloads(self):
        row = {
            "id": WORK_ORDER_ID,
            "question_id": QUESTION_ID,
            "plan_id": PLAN_ID,
            "analysis_job_id": JOB_ID,
            "skill_key": "thesis.targeted_challenge",
            "skill_version": 1,
            "skill_fingerprint": "b" * 64,
            "accepted_cutoff": NOW,
            "planning_policy_version": "v1",
            "estimated_value": 0.4,
            "reserved_cost_usd": 0,
            "reserved_runtime_seconds": 30,
            "status": "queued",
            "attempt_count": 0,
            "material_effect_summary": None,
            "error_kind": None,
            "created_at": NOW,
            "queued_at": NOW,
            "started_at": None,
            "completed_at": None,
        }
        session = Mock()
        session.execute.return_value = _Mappings(rows=[row])
        with (
            patch("routes.json.research.load_config", return_value=MOCK_CONFIG),
            patch(
                "routes.json.research.get_session",
                return_value=_session_context(session),
            ),
        ):
            response = client.get("/api/research/work-orders?limit=10", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["items"][0]
        self.assertNotIn("result", item)
        self.assertNotIn("payload", item)
        self.assertNotIn("input_fingerprint", item)
        sql = str(session.execute.call_args.args[0])
        self.assertNotIn(":status IS NULL", sql)
        self.assertNotIn(":question_id IS NULL", sql)

    def test_status_uses_bounded_aggregates_and_failures_are_generic(self):
        session = Mock()
        session.execute.side_effect = [
            _Mappings(rows=[{"status": "pending", "count": 3}]),
            _Mappings(
                one={
                    "active_work_orders": 2,
                    "latest_plan_at": NOW,
                    "latest_effect_at": None,
                    "stale_thesis_debt": 4,
                    "forecast_resolution_coverage": None,
                }
            ),
            _Mappings(first=None),
        ]
        with (
            patch("routes.json.research.load_config", return_value=MOCK_CONFIG),
            patch(
                "routes.json.research.get_session",
                return_value=_session_context(session),
            ),
        ):
            response = client.get("/api/research/control-plane/status", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["backlog"]["pending"], 3)
        self.assertEqual(response.json()["metrics"]["stale_thesis_debt"], 4)

        unavailable = Mock()
        unavailable.execute.side_effect = RuntimeError("password=private sql")
        with (
            patch("routes.json.research.load_config", return_value=MOCK_CONFIG),
            patch(
                "routes.json.research.get_session",
                return_value=_session_context(unavailable),
            ),
        ):
            response = client.get("/api/research/control-plane/status", headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertNotIn("sql", response.text)

    def test_manual_run_enforces_auth_csrf_and_coalesced_enqueue_contract(self):
        def enqueue(*_args, **kwargs):
            self.assertEqual(kwargs["trigger_kind"], "manual")
            self.assertEqual(kwargs["trigger_ref"], body["reason"])
            self.assertEqual(kwargs["dedupe_ref"], "global")
            return {
                "status": "coalesced",
                "job_id": str(JOB_ID),
                "created": False,
                "coalesced": True,
            }

        body = {"reason": "operator refresh"}
        self.assertEqual(
            client.post("/api/research/control-plane/run", json=body).status_code,
            401,
        )
        with (
            patch(
                "routes.json.triggers.get_budget_status",
                return_value={"available": True, "paid_calls_allowed": True},
            ),
            patch(
                "routes.json.research._enqueue_control_plane_planner",
                side_effect=enqueue,
            ),
            patch("routes.json.research.load_config", return_value=MOCK_CONFIG),
        ):
            response = client.post(
                "/api/research/control-plane/run", json=body, headers=AUTH
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["coalesced"])
        self.assertEqual(response.json()["analysis_job_id"], str(JOB_ID))

    def test_budget_override_requires_audit_before_enqueue(self):
        body = {
            "reason": "recover deferred work",
            "budget_override": True,
            "override_reason": "incident recovery",
        }
        order = []

        def register(**_kwargs):
            order.append("audit")
            return {"requested": True}

        def enqueue(*_args, **_kwargs):
            order.append("enqueue")
            return {
                "status": "accepted",
                "job_id": str(JOB_ID),
                "created": True,
                "coalesced": False,
            }

        with (
            patch(
                "routes.json.triggers.get_budget_status",
                return_value={"available": False, "status": "unavailable"},
            ),
            patch(
                "routes.json.research.register_manual_override", side_effect=register
            ),
            patch(
                "routes.json.research._enqueue_control_plane_planner",
                side_effect=enqueue,
            ),
            patch("routes.json.research.load_config", return_value=MOCK_CONFIG),
        ):
            response = client.post(
                "/api/research/control-plane/run", json=body, headers=AUTH
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(order, ["audit", "enqueue"])


if __name__ == "__main__":
    unittest.main()
