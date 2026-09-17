import os
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

os.environ["DEPLOYMENT_MODE"] = "test"

from contracts import (
    OrchestratorHealthResponse,
    RunAcceptanceRequest,
    RunAcceptedResponse,
    RunDetailResponse,
    SystemTopologyNode,
    SystemTopologyResponse,
)


class SharedContractTests(unittest.TestCase):
    def test_health_schema_exposes_stable_contract_fields(self):
        schema = OrchestratorHealthResponse.model_json_schema()
        self.assertEqual(
            set(schema["properties"]),
            {
                "liveness",
                "readiness",
                "data_health",
                "status",
                "components",
                "scheduler",
                "stream",
                "collectors",
                "quality",
                "config_version",
            },
        )
        self.assertEqual(schema["type"], "object")

    def test_health_rejects_malformed_enum_and_component_fields(self):
        payload = {
            "liveness": "ok",
            "readiness": "ready",
            "data_health": "healthy",
            "components": [{"name": "db", "status": "corrupted"}],
        }
        with self.assertRaises(ValidationError):
            OrchestratorHealthResponse.model_validate(payload)

    def test_run_acceptance_rejects_wrong_field_types(self):
        with self.assertRaises(ValidationError):
            RunAcceptanceRequest.model_validate(
                {"mode": "refresh", "budget_confirmed": "yes"}
            )

    def test_run_response_requires_job_id_and_accepted_at(self):
        with self.assertRaises(ValidationError):
            RunAcceptedResponse.model_validate({"job_id": "run-1"})

    def test_run_detail_accepts_filings_producer_kind(self):
        response = RunDetailResponse.model_validate(
            {
                "correlation_id": "filings-run",
                "status": "completed",
                "run_kind": "filings",
            }
        )
        self.assertEqual(response.run_kind, "filings")

    def test_topology_is_bounded_strict_and_timezone_safe(self):
        node = SystemTopologyNode(
            id="planner",
            label="Planner",
            group="Research",
            kind="planner",
            status="idle",
            activity_state="no eligible questions",
            bounded_count=0,
            safe_detail="Latest persisted plan selected no work.",
        )
        response = SystemTopologyResponse(
            generated_at=datetime.now(UTC),
            status="available",
            nodes=[node],
            edges=[],
            summary="One bounded topology node.",
        )
        self.assertEqual(response.nodes[0].bounded_count, 0)

        with self.assertRaisesRegex(ValidationError, "timezone-aware"):
            SystemTopologyResponse(
                generated_at=datetime.now(),
                status="available",
                nodes=[],
                edges=[],
                summary="Unavailable.",
            )
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            SystemTopologyNode(
                id="planner",
                label="Planner",
                group="Research",
                kind="planner",
                status="idle",
                activity_state="idle",
                safe_detail="No work.",
                private_payload={"token": str(uuid4())},
            )


if __name__ == "__main__":
    unittest.main()
