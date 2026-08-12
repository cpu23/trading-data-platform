import os
import unittest

from pydantic import ValidationError

os.environ["DEPLOYMENT_MODE"] = "test"

from contracts import (
    OrchestratorHealthResponse,
    RunAcceptanceRequest,
    RunAcceptedResponse,
    RunDetailResponse,
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

    def test_openapi_routes_reference_shared_response_models(self):
        from main import app

        schema = app.openapi()
        health_schema = schema["paths"]["/api/system/health"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
        cycle_schema = schema["paths"]["/api/triggers/cycle"]["post"]["responses"][
            "202"
        ]["content"]["application/json"]["schema"]
        collect_schema = schema["paths"]["/api/collect/{source_id}"]["post"][
            "responses"
        ]["202"]["content"]["application/json"]["schema"]

        self.assertEqual(
            health_schema["$ref"], "#/components/schemas/SystemHealthResponse"
        )
        self.assertEqual(
            cycle_schema["$ref"], "#/components/schemas/RunAcceptedResponse"
        )
        self.assertEqual(
            collect_schema["$ref"], "#/components/schemas/RunAcceptedResponse"
        )


if __name__ == "__main__":
    unittest.main()
